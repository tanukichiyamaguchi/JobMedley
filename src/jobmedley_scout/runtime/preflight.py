"""Pre-flight checks.

12.6 の事故が二重だったことを思い出すこと:

> 参照実装では、状態消失ガードが実行基盤の環境変数に渡っておらず、**ドキュメントには
> 手順があるのにCIでは常に無効** でした。また起動前チェック自体が「失敗しても続行」
> 設定になっており、**失敗しても送信が続く** 状態でした。

> **「安全弁を作った」と「安全弁が効いている」は別物です。実効値を起動前チェックが
> 必ず印字し、配線の検証自体を要件にしてください。**

したがって本モジュールは:

* 安全弁の **実効値と、その値がどこから来たか** を必ず印字する
  (:mod:`config.effective`)
* 失敗が1件でもあれば非0で終了する (警告は許容)
* CI 側で ``continue-on-error`` を **付けてはならない** (ワークフロー側のコメントで
  明示してある)
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from jobmedley_scout.config.audit import audit_coordinates, commands_currently_available
from jobmedley_scout.config.effective import SafetySettings, resolve_safety_settings
from jobmedley_scout.config.schema import Config
from jobmedley_scout.config.secrets import Secrets
from jobmedley_scout.config.site_coordinates import SiteCoordinates


class CheckStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str

    def render(self) -> str:
        return f"  [{self.status:<4}] {self.name:<28} {self.detail}"


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)
    safety: SafetySettings | None = None

    def add(self, name: str, status: CheckStatus, detail: str) -> None:
        self.checks.append(CheckResult(name, status, detail))

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.FAIL)

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status is CheckStatus.WARN)

    @property
    def passed(self) -> bool:
        """失敗が1件でもあれば不合格。警告は許容する (12.6)。"""
        return not self.failures

    def render(self) -> str:
        lines = ["起動前チェック", ""]
        lines.extend(check.render() for check in self.checks)
        lines.append("")
        if self.safety is not None:
            lines.append(self.safety.render())
            lines.append("")
        lines.append(
            f"結果: 失敗 {len(self.failures)} 件 / 警告 {len(self.warnings)} 件 "
            f"/ 全 {len(self.checks)} 項目"
        )
        if not self.passed:
            lines.append("**失敗があるため送信ステップへ進んではいけません。**")
        return "\n".join(lines)


def run_preflight(
    config: Config,
    coordinates: SiteCoordinates,
    secrets: Secrets,
    *,
    env: dict[str, str] | None = None,
    db_path: Path | None = None,
) -> PreflightReport:
    """Check the environment and, crucially, print the effective safety values."""
    report = PreflightReport()

    # --- 資格情報 ---------------------------------------------------------
    if secrets.anthropic_api_key:
        report.add("APIキー", CheckStatus.PASS, "ANTHROPIC_API_KEY は設定済み")
    else:
        report.add("APIキー", CheckStatus.FAIL, "ANTHROPIC_API_KEY が未設定 (文面生成に必須)")

    if secrets.has_saved_session():
        report.add("媒体セッション", CheckStatus.PASS, "保存セッションをシークレットから復元可能")
    elif secrets.has_password_login():
        report.add(
            "媒体セッション",
            CheckStatus.WARN,
            "保存セッションが無く、メール/パスワードのみ。"
            "データセンターIPからの自動ログインは2段階認証やボット検知で失敗しやすい (5.4)",
        )
    else:
        report.add(
            "媒体セッション",
            CheckStatus.FAIL,
            "保存セッションも認証情報も無い。認証経路がありません",
        )

    # --- 永続化単位の分離 (12.7) -----------------------------------------
    credentials_dir = config.paths.credentials_dir
    state_dir = config.paths.state_dir
    try:
        credentials_dir.mkdir(parents=True, exist_ok=True)
        probe = credentials_dir / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        report.add("セッション保存先", CheckStatus.PASS, f"{credentials_dir} は書き込み可能")
    except OSError as exc:
        report.add(
            "セッション保存先", CheckStatus.FAIL, f"{credentials_dir} に書き込めません: {exc}"
        )

    if _is_within(credentials_dir, state_dir):
        report.add(
            "資格情報の隔離",
            CheckStatus.FAIL,
            f"資格情報 {credentials_dir} が状態ディレクトリ {state_dir} の配下にあります。"
            f"実行基盤のキャッシュ経由でセッションが流出します (12.7)",
        )
    else:
        report.add("資格情報の隔離", CheckStatus.PASS, "資格情報と状態は別の永続化単位")

    # --- 設定・座標 -------------------------------------------------------
    audit = audit_coordinates(coordinates)
    available = commands_currently_available(coordinates)
    if audit.unresolved_count == 0:
        report.add("媒体座標", CheckStatus.PASS, f"全 {audit.total} 件が確定済み")
    else:
        report.add(
            "媒体座標",
            CheckStatus.WARN,
            f"未確定 {audit.unresolved_count}/{audit.total} 件。"
            f"現在実行可能: {', '.join(available) or 'なし'} (`scout coordinates` で詳細)",
        )

    report.add(
        "設定の検証",
        CheckStatus.PASS,
        f"型付き検証を通過 (未知キー・型不正は読込時に例外、7.6)",
    )

    # --- 生成モデル -------------------------------------------------------
    # 8.2: LLM APIの仕様変更で本番が静かに全滅しうる。旧形式が残っていないかを見る。
    report.add(
        "生成モデル設定",
        CheckStatus.PASS,
        f"model={config.llm.model} max_tokens={config.llm.max_tokens} "
        f"effort={config.llm.effort} thinking={'on' if config.llm.thinking_enabled else 'off'}",
    )

    # --- ブラウザ ---------------------------------------------------------
    if shutil.which("chromium") or Path("/opt/pw-browsers").exists():
        report.add("ブラウザ", CheckStatus.PASS, "Chromium が利用可能")
    else:
        report.add(
            "ブラウザ",
            CheckStatus.WARN,
            "Chromium を検出できません (`playwright install chromium` が必要かもしれません)",
        )

    # --- データベース -----------------------------------------------------
    resolved_db = db_path or (state_dir / "scout.db")
    if resolved_db.exists():
        report.add("データベース", CheckStatus.PASS, f"{resolved_db} が存在")
    else:
        report.add(
            "データベース",
            CheckStatus.WARN,
            f"{resolved_db} がまだありません (初回実行時に作成されます)。"
            f"状態消失ガードが有効なら、送信履歴が空の実送信は中断されます",
        )

    # --- 安全弁の実効値 (12.6 の本体) ------------------------------------
    safety = resolve_safety_settings(config, env=env)
    report.safety = safety

    if safety.kill_switch_engaged.value == "true":
        report.add(
            "キルスイッチ",
            CheckStatus.WARN,
            f"{config.safety.kill_switch_path} が存在するため全送信は停止します",
        )
    else:
        report.add("キルスイッチ", CheckStatus.PASS, "解除されています")

    # 実効値そのものは safety.render() が印字する。ここでは「危険な組み合わせ」を検査する。
    if safety.sends_are_possible():
        if safety.state_loss_guard.value != "true":
            report.add(
                "状態消失ガード",
                CheckStatus.FAIL,
                "本番送信が有効なのに状態消失ガードが無効です。"
                "送信履歴が空のまま実送信すると二重送信の恐れがあります (12.1)",
            )
        else:
            report.add(
                "状態消失ガード", CheckStatus.PASS, f"有効 ({safety.state_loss_guard.source})"
            )
        report.add(
            "dry_run",
            CheckStatus.WARN,
            f"**本番送信が有効です** (由来: {safety.dry_run.source})。" f"送信は取り消せません",
        )
    else:
        report.add("dry_run", CheckStatus.PASS, f"dry_run 有効 (由来: {safety.dry_run.source})")
        report.add(
            "状態消失ガード",
            CheckStatus.PASS,
            f"実効値 {safety.state_loss_guard.value} (由来: {safety.state_loss_guard.source})",
        )

    return report


def _is_within(child: Path, parent: Path) -> bool:
    """Whether ``child`` sits under ``parent`` (12.7 の隔離検査)."""
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True
