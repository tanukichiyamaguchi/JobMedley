"""Effective safety settings, with the source of each value.

12.6 の事故:

> 状態消失ガードが実行基盤の環境変数に渡っておらず、**ドキュメントには手順が
> あるのにCIでは常に無効** だった。また起動前チェック自体が「失敗しても続行」
> 設定になっており、**失敗しても送信が続く** 状態だった。

> **「安全弁を作った」と「安全弁が効いている」は別物です。実効値を起動前
> チェックが必ず印字し、配線の検証自体を要件にしてください。**

したがって本モジュールは値だけでなく **どこから来た値か** (設定ファイル / 環境
変数 / 上書きなし) を持ち、起動前チェックがそれを印字する。「環境変数で上書き
しているつもりだったが実は届いていなかった」が目視で分かるようにするため。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jobmedley_scout.config.schema import Config
from jobmedley_scout.errors import ConfigError

ENV_DRY_RUN = "SCOUT_DRY_RUN"
ENV_STATE_LOSS_GUARD = "SCOUT_STATE_LOSS_GUARD"
ENV_SEND_CAP_PAID = "SCOUT_SEND_CAP_PAID"
ENV_SEND_CAP_FREE = "SCOUT_SEND_CAP_FREE"

SOURCE_CONFIG = "config.yaml"


@dataclass(frozen=True)
class EffectiveValue:
    """One safety value, plus where it actually came from."""

    name: str
    value: str
    source: str

    def render(self) -> str:
        return f"{self.name:<22} = {self.value:<28} (由来: {self.source})"


@dataclass(frozen=True)
class SafetySettings:
    """The four valves whose effective values preflight must print."""

    dry_run: EffectiveValue
    state_loss_guard: EffectiveValue
    send_cap_paid: EffectiveValue
    send_cap_free: EffectiveValue
    kill_switch_path: EffectiveValue
    kill_switch_engaged: EffectiveValue

    def all_values(self) -> tuple[EffectiveValue, ...]:
        return (
            self.dry_run,
            self.state_loss_guard,
            self.send_cap_paid,
            self.send_cap_free,
            self.kill_switch_path,
            self.kill_switch_engaged,
        )

    def render(self) -> str:
        lines = ["安全弁の実効値 (12.6: 作ったことと効いていることは別物)"]
        lines.extend(f"  {value.render()}" for value in self.all_values())
        return "\n".join(lines)

    def sends_are_possible(self) -> bool:
        """Whether a real send could happen with these settings."""
        return self.dry_run.value == "false" and self.kill_switch_engaged.value == "false"


def _parse_bool(name: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes", "on"):
        return True
    if lowered in ("false", "0", "no", "off"):
        return False
    # 曖昧な値を黙って false にしない。dry_run="maybe" が「本番送信」に
    # 解釈されるのが最悪の失敗なので、解釈できなければ止める。
    raise ConfigError(
        f"環境変数 {name} の値 {raw!r} を真偽値として解釈できません "
        f"(true/false で指定してください)"
    )


def _parse_int(name: str, raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"環境変数 {name} の値 {raw!r} を整数として解釈できません") from exc


def _bool_setting(
    name: str, env_key: str, config_value: bool, env: Mapping[str, str]
) -> EffectiveValue:
    raw = env.get(env_key, "").strip()
    if raw:
        return EffectiveValue(name, str(_parse_bool(env_key, raw)).lower(), f"env:{env_key}")
    return EffectiveValue(name, str(config_value).lower(), SOURCE_CONFIG)


def _int_setting(
    name: str, env_key: str, config_value: int, env: Mapping[str, str]
) -> EffectiveValue:
    raw = env.get(env_key, "").strip()
    if raw:
        return EffectiveValue(name, str(_parse_int(env_key, raw)), f"env:{env_key}")
    return EffectiveValue(name, str(config_value), SOURCE_CONFIG)


def resolve_safety_settings(config: Config, env: Mapping[str, str] | None = None) -> SafetySettings:
    """Compute the effective safety settings for this run."""
    source = os.environ if env is None else env
    kill_switch: Path = config.safety.kill_switch_path
    engaged = kill_switch.exists()

    return SafetySettings(
        dry_run=_bool_setting("dry_run", ENV_DRY_RUN, config.safety.dry_run, source),
        state_loss_guard=_bool_setting(
            "state_loss_guard", ENV_STATE_LOSS_GUARD, config.safety.state_loss_guard, source
        ),
        send_cap_paid=_int_setting(
            "send_cap_paid", ENV_SEND_CAP_PAID, config.send.per_run_cap_paid, source
        ),
        send_cap_free=_int_setting(
            "send_cap_free", ENV_SEND_CAP_FREE, config.send.per_run_cap_free, source
        ),
        kill_switch_path=EffectiveValue("kill_switch_path", str(kill_switch), SOURCE_CONFIG),
        kill_switch_engaged=EffectiveValue(
            "kill_switch_engaged",
            str(engaged).lower(),
            "ファイルの存在" if engaged else "ファイル無し",
        ),
    )


def effective_dry_run(settings: SafetySettings) -> bool:
    return settings.dry_run.value == "true"


def effective_state_loss_guard(settings: SafetySettings) -> bool:
    return settings.state_loss_guard.value == "true"


def effective_cap(settings: SafetySettings, slot: str) -> int:
    if slot == "paid":
        return int(settings.send_cap_paid.value)
    if slot == "free":
        return int(settings.send_cap_free.value)
    # 9.7: 枠ごとに上限を持つ。未知の枠に既定の上限を与えると、
    # 「不明枠」が無制限に送れてしまう。
    raise ConfigError(f"送信枠 {slot!r} の上限が定義されていません")
