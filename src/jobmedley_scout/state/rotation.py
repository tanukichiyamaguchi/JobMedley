"""Rotation ordering and batch selection (9.6).

参照実装の事故 -- **エラーを一切出さないバグ**:

> 返信チェックの対象抽出が「送信の古い順に上限件数」の **固定順** だったため、
> 対象が上限を超えた時点から毎回同じ最古の対象だけを再訪し、**返信が最も来やすい
> 直近の送信者を永久に見逃していた。**

固定順が悪いのではなく、「上限で切った残りが次回に回らない」ことが悪い。だから
順序に **前回チェック時刻** を持ち込み、未チェック → 最も昔にチェックしたもの、
の順に回す。これで N 回の実行で必ず全件を一巡する。

**上限は「1回の実行時間」を守るためのものであって「対象を絞る」ためではない。**
上限で落ちた分は捨てられたのではなく次回に回る、という不変条件がこのモジュールの
存在理由であり、:class:`RotationBatch` が ``total_candidates`` と ``truncated`` を
必ず持つのは 12.8 の「対象N件・処理M件」を **毎回ログに出させる** ためである
(件数を出さない限り、この種のバグは誰も気づけない)。

永続化は :mod:`state.rotation_repo` が担当する。ここは純粋な並べ替えと選択だけ。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

#: 未チェック (``last_processed_at is None``) を並べるときの番兵。
#: 実在の時刻と比較されることはない -- 並べ替えキーの第1要素で群が分かれるため。
_NEVER = datetime.min.replace(tzinfo=UTC)


class RotationResult(StrEnum):
    """What happened to one entry when the caller processed it.

    ``SKIPPED`` と ``ERROR`` を独立に持つのは、**どちらでもカーソルを前進させる**
    ことを型の上で明示するため (9.6)。成功時だけ前進させると、失敗する対象が
    バッチの先頭に居座り、後続が永久に処理されない。
    """

    PROCESSED = "processed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class RotationEntry:
    """One candidate's position in a rotation scope."""

    candidate_id: str
    #: 未チェックなら ``None``。この ``None`` が「最優先」を意味する (9.6)。
    last_processed_at: datetime | None = None
    last_result: RotationResult | None = None

    @property
    def never_processed(self) -> bool:
        return self.last_processed_at is None


@dataclass(frozen=True)
class RotationBatch:
    """The slice of a rotation scope to process in this run.

    ``total_candidates`` と ``truncated`` は飾りではない。9.6 のバグは例外も
    エラーログも出さず、「対象N件・処理M件」の件数ログだけが症状を見せた。
    したがって件数は結果オブジェクトに載せて **返し**、呼び出し側が出力し忘れない
    ようにする (12.8)。
    """

    selected: tuple[str, ...]
    #: この巡回スコープの全件数 (上限で切る前)。
    total_candidates: int
    #: 今回処理されない件数。**捨てられたのではなく次回に回る。**
    truncated: int
    #: 一度もチェックされていない件数。増え続けるなら一巡できていない。
    never_processed: int
    limit: int

    @property
    def was_truncated(self) -> bool:
        return self.truncated > 0

    def describe(self) -> str:
        """The count line that makes 9.6 visible. Always log it."""
        line = f"対象{self.total_candidates}件・処理{len(self.selected)}件"
        if self.was_truncated:
            line += (
                f"・未処理{self.truncated}件 (上限{self.limit}件。"
                f"未処理分は破棄ではなく次回実行に回る)"
            )
        if self.never_processed:
            line += f"・未チェック{self.never_processed}件"
        return line


def _sort_key(entry: RotationEntry) -> tuple[int, datetime, str]:
    """Never-checked first, then longest-ago, then id for determinism."""
    stamp = entry.last_processed_at
    if stamp is None:
        return (0, _NEVER, entry.candidate_id)
    if stamp.tzinfo is None:
        raise ValueError(
            f"naive datetime on rotation entry {entry.candidate_id!r}; "
            "every instant in this system is tz-aware"
        )
    # 同時刻の並びを候補者IDで確定させる。順序が非決定的だと、上限で切ったときに
    # 「毎回同じ対象が落ちる」状況が再現できず、9.6 の再発をテストで押さえられない。
    return (1, stamp.astimezone(UTC), entry.candidate_id)


def order_for_rotation(entries: Iterable[RotationEntry]) -> tuple[RotationEntry, ...]:
    """Rotation order: never-checked first, then longest-ago first.

    未チェックを先頭に置くのは、新規追加された対象が「常に最後尾」になって
    永久に届かない事態を防ぐため (9.6 と同じ形の飢餓)。
    """
    return tuple(sorted(entries, key=_sort_key))


def select_batch(entries: Iterable[RotationEntry], limit: int) -> RotationBatch:
    """Take the first ``limit`` entries in rotation order, reporting the counts.

    上限は実行時間を守るための値であって、対象を絞るための値ではない。よって
    切り落とした件数を必ず返す -- 黙って捨てると 9.6 と同じ「エラーの出ない
    取りこぼし」になる。
    """
    if limit < 1:
        raise ValueError("上限は1件以上でなければならない (0だとバッチが永久に空になる)")
    ordered = order_for_rotation(entries)
    selected = ordered[:limit]
    return RotationBatch(
        selected=tuple(entry.candidate_id for entry in selected),
        total_candidates=len(ordered),
        truncated=max(0, len(ordered) - len(selected)),
        never_processed=sum(1 for entry in ordered if entry.never_processed),
        limit=limit,
    )


def advance_cursor(entry: RotationEntry, result: RotationResult, now: datetime) -> RotationEntry:
    """Move one cursor forward.

    **スキップでもエラーでも前進させる。** 成功時だけ前進させると、失敗し続ける
    対象がローテーションの先頭に居座り、バッチがその対象で詰まって後続が永久に
    処理されない (9.6)。だから ``result`` は「前進するかどうか」の条件ではなく、
    記録される値でしかない。
    """
    if now.tzinfo is None:
        raise ValueError("naive datetime; every instant in this system is tz-aware")
    return replace(entry, last_processed_at=now.astimezone(UTC), last_result=result)


def advance_batch(
    entries: Iterable[RotationEntry],
    processed_ids: Iterable[str],
    result: RotationResult,
    now: datetime,
) -> tuple[RotationEntry, ...]:
    """Advance every cursor in ``processed_ids``, leaving the rest untouched.

    入力の順序は保つ (並べ替えは :func:`order_for_rotation` の仕事)。
    """
    targets = set(processed_ids)
    return tuple(
        advance_cursor(entry, result, now) if entry.candidate_id in targets else entry
        for entry in entries
    )
