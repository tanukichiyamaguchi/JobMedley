"""Coordinate audit: what is still unknown, and what it blocks.

``scout coordinates`` の実体。未確定の座標を **ラダー段階別に** 並べ、それぞれの
取得方法と「埋めると何ができるようになるか」を印字する。運用者がコードを読まずに
次の一手を決められるようにするのが目的。

:func:`assert_ready_for` はコマンド開始時のゲート。保護4層のうちの3層目で、
「送信コマンドが、送信先URLが未確定のまま走り出す」のを止める。
"""

from __future__ import annotations

from dataclasses import dataclass

from jobmedley_scout.config.coordinates import (
    COORDINATES,
    COORDINATES_BY_KEY,
    REQUIRED_BY_COMMAND,
    commands_unblocked_by,
)
from jobmedley_scout.config.placeholders import LadderStage, Unresolved
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.errors import ConfigError, UnresolvedCoordinateError


@dataclass(frozen=True)
class CoordinateAudit:
    total: int
    resolved: tuple[str, ...]
    unresolved: tuple[str, ...]

    @property
    def resolved_count(self) -> int:
        return len(self.resolved)

    @property
    def unresolved_count(self) -> int:
        return len(self.unresolved)


def audit_coordinates(coordinates: SiteCoordinates) -> CoordinateAudit:
    return CoordinateAudit(
        total=len(COORDINATES),
        resolved=coordinates.resolved_keys(),
        unresolved=coordinates.unresolved_keys(),
    )


def assert_ready_for(coordinates: SiteCoordinates, command: str) -> None:
    """Raise if ``command`` needs a coordinate that is still unconfirmed.

    黙って0件で成功するのではなく、**明示的に停止する** ことが要件 (原則2)。
    """
    required = REQUIRED_BY_COMMAND.get(command)
    if required is None:
        raise ConfigError(
            f"コマンド '{command}' の必須座標が宣言されていません。"
            f"config/coordinates.py の REQUIRED_BY_COMMAND に追加してください。"
        )
    blocking = sorted(key for key in required if coordinates.is_unresolved(key))
    if not blocking:
        return
    first = COORDINATES_BY_KEY[blocking[0]]
    detail = "\n".join(
        f"  - {key}  [{COORDINATES_BY_KEY[key].stage}]\n"
        f"      {COORDINATES_BY_KEY[key].how_to_obtain}"
        for key in blocking
    )
    # 集約報告だが Unresolved をそのまま使う。エラー本文の体裁を共有できるし、
    # 「未確定座標のせいで止まった」という意味は単数でも複数でも同じだから。
    raise UnresolvedCoordinateError(
        Unresolved(
            key=f"{len(blocking)}件の未確定座標",
            stage=first.stage,
            how_to_obtain=(
                f"コマンド '{command}' は以下の座標を必要とします:\n{detail}\n"
                f"  `scout coordinates` で全体像を確認できます。"
            ),
        ),
        used_by=f"command:{command}",
    )


def render_audit(coordinates: SiteCoordinates) -> str:
    """Human-readable report grouped by ladder stage."""
    audit = audit_coordinates(coordinates)
    lines = [
        "媒体座標の確定状況",
        f"  確定 {audit.resolved_count} / {audit.total} 件"
        f"  (未確定 {audit.unresolved_count} 件)",
        "",
    ]
    if not audit.unresolved:
        lines.append("すべての座標が確定しています。")
        return "\n".join(lines)

    unresolved = set(audit.unresolved)
    for stage in LadderStage:
        stage_keys = [
            spec.key for spec in COORDINATES if spec.stage is stage and spec.key in unresolved
        ]
        if not stage_keys:
            continue
        lines.append(f"── {stage} ── ({len(stage_keys)}件)")
        for key in stage_keys:
            spec = COORDINATES_BY_KEY[key]
            unblocks = commands_unblocked_by(key)
            lines.append(f"  {key}")
            lines.append(f"    取得方法: {spec.how_to_obtain}")
            if unblocks:
                lines.append(f"    解禁されるコマンド: {', '.join(unblocks)}")
            if spec.nullable:
                lines.append("    (存在しないことが確認できた場合は null と書いてよい)")
        lines.append("")

    lines.append("値が確定するまでは 'UNRESOLVED' のままにしてください。")
    lines.append("推測で埋めると、その推測がそのまま送信内容と対象判定に出ます。")
    return "\n".join(lines)


def commands_currently_available(coordinates: SiteCoordinates) -> tuple[str, ...]:
    """Commands whose required coordinates are all confirmed."""
    available: list[str] = []
    for command, required in REQUIRED_BY_COMMAND.items():
        if not any(coordinates.is_unresolved(key) for key in required):
            available.append(command)
    return tuple(sorted(available))
