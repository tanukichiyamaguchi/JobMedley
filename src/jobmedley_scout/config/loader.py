"""Configuration loading.

**パースは寛容に、破壊は厳格に** (7.7) の「厳格」側がここ。読み込みは以下の
どれか一つでも起きたら例外にする:

* 未知のキーがある (打鍵ミス)
* 必須キーが無い (書き忘れ)
* 型が合わない
* 座標ファイルに登録外のキーがある / 登録済みのキーが欠けている

7.6 の通り、**検証レイヤの導入時は「検証後の値が元のファイルと完全一致する」
テストで振る舞い不変を証明してから入れる**こと。検証が静かに既定値を注入して
判定を変える二次事故を防ぐため。そのテストは tests/config/test_roundtrip.py。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from jobmedley_scout.config.coordinates import COORDINATES, COORDINATES_BY_KEY, CoordKind
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN, Unresolved
from jobmedley_scout.config.schema import Config
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.ids import IdPattern, IdPatternKind, configure_id_patterns


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"設定ファイルが見つかりません: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} のYAMLが不正です: {exc}") from exc
    if loaded is None:
        raise ConfigError(f"{path} が空です")
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} のトップレベルはマッピングである必要があります")
    return loaded


def load_behavior_config(path: Path) -> Config:
    """Load and validate ``config.yaml``."""
    raw = _read_yaml(path)
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        # pydantic のエラーはそのまま出すと読みにくいので、キーの位置を先頭に出す。
        lines = [f"{path} の検証に失敗しました:"]
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            lines.append(f"  - {location}: {error['msg']}")
        raise ConfigError("\n".join(lines)) from exc


def _parse_coordinate(key: str, kind: CoordKind, value: object) -> object:
    if kind in (CoordKind.URL, CoordKind.SELECTOR, CoordKind.STRING, CoordKind.JSON_PATH):
        if not isinstance(value, str):
            raise ConfigError(
                f"座標 '{key}' は文字列である必要があります (実際: {type(value).__name__})"
            )
        return value
    if kind is CoordKind.BOOL:
        if not isinstance(value, bool):
            raise ConfigError(f"座標 '{key}' は true/false である必要があります")
        return value
    if kind is CoordKind.INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"座標 '{key}' は整数である必要があります")
        return value
    if kind is CoordKind.STATUS_SET:
        if not isinstance(value, list) or not all(
            isinstance(item, int) and not isinstance(item, bool) for item in value
        ):
            raise ConfigError(
                f"座標 '{key}' はHTTPステータスの配列である必要があります (例: [200, 201])"
            )
        return frozenset(int(item) for item in value)
    if kind is CoordKind.STRING_LIST:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"座標 '{key}' は文字列の配列である必要があります")
        return tuple(str(item) for item in value)
    if kind is CoordKind.ENUM_MAP:
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise ConfigError(f"座標 '{key}' は文字列→文字列のマッピングである必要があります")
        return {str(k): str(v) for k, v in value.items()}
    raise ConfigError(f"座標 '{key}' の種別 {kind} を解釈できません")  # pragma: no cover


def load_site_coordinates(path: Path) -> SiteCoordinates:
    """Load ``site_coordinates.yaml``.

    **全キーが必ず存在すること** を要求する。キーの省略は打鍵ミスと同じく検証
    エラーであり、「うっかり既定値に落ちる」経路が存在しない (7.6)。
    """
    raw = _read_yaml(path)

    registered = set(COORDINATES_BY_KEY)
    present = set(raw)

    unknown = sorted(present - registered)
    if unknown:
        raise ConfigError(
            f"{path} に未登録の座標キーがあります (打鍵ミスの可能性): {', '.join(unknown)}"
        )
    missing = sorted(registered - present)
    if missing:
        raise ConfigError(
            f"{path} に必須の座標キーが不足しています。"
            f"未確定なら値に '{UNRESOLVED_TOKEN}' と明記してください: {', '.join(missing)}"
        )

    parsed: dict[str, object] = {}
    for spec in COORDINATES:
        value = raw[spec.key]
        if value == UNRESOLVED_TOKEN:
            parsed[spec.key] = Unresolved(spec.key, spec.stage, spec.how_to_obtain)
            continue
        if value is None:
            if not spec.nullable:
                raise ConfigError(
                    f"座標 '{spec.key}' に null は指定できません。"
                    f"未確定なら '{UNRESOLVED_TOKEN}' と書いてください "
                    f"(null は「確認した結果 存在しない」という確定した答えを意味します)"
                )
            parsed[spec.key] = None
            continue
        parsed[spec.key] = _parse_coordinate(spec.key, spec.kind, value)

    return SiteCoordinates(parsed)


def _build_id_patterns(config: Config) -> tuple[IdPattern, ...]:
    patterns: list[IdPattern] = []
    for entry in config.ids.observed_patterns:
        try:
            name = entry["name"]
            kind = IdPatternKind(entry["kind"])
        except KeyError as exc:
            raise ConfigError(f"ids.observed_patterns の項目に {exc} がありません") from exc
        except ValueError as exc:
            raise ConfigError(f"ids.observed_patterns の kind が不正です: {exc}") from exc
        patterns.append(IdPattern(name=name, kind=kind, argument=entry.get("argument", "")))
    return tuple(patterns)


def load_all(config_path: Path, coordinates_path: Path) -> tuple[Config, SiteCoordinates]:
    """Load both files and install process-wide derived settings.

    ID正規化パターンの適用はここで行う。pydantic のバリデータに載せることが
    「取り込み経路の書き忘れ」を構造的に排除する唯一の手段なので、その前提と
    なるパターン設定は起動時に一度だけ確定させる (9.3)。
    """
    config = load_behavior_config(config_path)
    coordinates = load_site_coordinates(coordinates_path)
    configure_id_patterns(_build_id_patterns(config))
    return config, coordinates
