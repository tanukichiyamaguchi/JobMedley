"""Deterministic full-table rewrite (11.1).

**全量書き換えである。** 差分更新にしないのは、誤検知や集計バグを直したときに
古い行が残らないようにするため (10.4 と同じ理屈: 上書き型でないと自己修復できない)。
同じ入力からは必ず同じ表が出る -- 時刻も乱数も行番号も混ぜていないので、2回続けて
実行すれば1バイトも変わらない (テストで表明)。

入力列の値を持つフィールドが :class:`DisplayRow` に **存在しない** ことが、
このモジュールの一番重要な設計である。計算した値を人間の入力欄に流し込む経路が
コード上に無ければ、11.1 の往復ループは構造的に閉じられない。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from jobmedley_scout.analytics.aggregate import (
    CohortRow,
    CohortTable,
    RateStatus,
    ReplyRate,
    monthly_table,
    weekly_table,
)
from jobmedley_scout.analytics.cohort import Granularity
from jobmedley_scout.analytics.sheet_schema import (
    ALL_COLUMNS,
    ROW_KEY_COLUMN,
    SLOT_COLUMN_KEYS,
    ColumnRole,
    blank_input_cell,
)
from jobmedley_scout.clock import Clock
from jobmedley_scout.config.schema import AnalyticsConfig
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.reply import ReplyDetection
from jobmedley_scout.models.send_record import SendRecord

#: 直近コホートの注記 (11.3)。これが無いと運用者は毎週「今週は悪化した」と読む。
RECENT_NOTE_WEEKLY: Final[str] = "直近週: 返信がまだ届き得るため低めに出ます"
RECENT_NOTE_MONTHLY: Final[str] = "直近月: 返信がまだ届き得るため低めに出ます"


def format_rate(rate: ReplyRate) -> str:
    """Render a reply rate as a percentage number, or empty when it has no value.

    値が無いときは空文字にする。``0`` や ``-`` を入れないこと -- 空セルは
    グラフで「点を打たない」になるが、``0`` は「返信率0%の週」として描かれる。
    埋められない値を既定値に寄せると、グラフが嘘をつく。
    """
    if rate.status is not RateStatus.COMPUTED or rate.value is None:
        return ""
    return f"{rate.value * 100:.1f}"


@dataclass(frozen=True)
class DisplayRow:
    """One row of the display projection.

    **入力列の値を保持するフィールドは意図的に無い** (11.1)。``values`` は表示列の
    キーだけを持ち、:meth:`cells` が入力列の位置に必ず空文字を書く。ここに
    ``input_values`` を足したくなったら、それは往復ループを再導入する変更である。
    """

    row_key: str
    #: 表示列のみ。(key, rendered value) の組で、表示列の宣言順。
    values: tuple[tuple[str, str], ...]

    def value(self, key: str) -> str:
        for stored_key, stored_value in self.values:
            if stored_key == key:
                return stored_value
        raise ConfigError(f"表示行に列 {key!r} がありません")

    def cells(self) -> tuple[str, ...]:
        """The full row in sheet column order, with input columns always empty.

        列の役割で書き分ける。入力列に何を書くかを呼び出し側に選ばせない
        (選べるようにした瞬間、いつか値が入る)。
        """
        rendered: dict[str, str] = dict(self.values)
        cells: list[str] = []
        for column in ALL_COLUMNS:
            if column.role is ColumnRole.INPUT:
                cells.append(blank_input_cell())
                continue
            if column.key not in rendered:
                raise ConfigError(f"表示列 {column.key!r} の値が生成されていません")
            cells.append(rendered[column.key])
        return tuple(cells)


def row_from_cohort(row: CohortRow) -> DisplayRow:
    """Project one aggregated cohort into display cells."""
    note = ""
    if row.is_recent:
        note = RECENT_NOTE_WEEKLY if row.granularity is Granularity.WEEKLY else RECENT_NOTE_MONTHLY

    values: list[tuple[str, str]] = [
        (ROW_KEY_COLUMN, row.key),
        ("sent", str(row.sent)),
        ("replies", str(row.replies)),
        ("reply_rate_pct", format_rate(row.reply_rate)),
    ]
    # 枠ごとの列は写像から引く。ここで文字列を直書きすると、枠が増えたときに
    # 集計だけ増えて列に出ない状態になる (9.4)。
    for slot, columns in SLOT_COLUMN_KEYS.items():
        breakdown = row.slot(slot)
        values.append((columns.sent, str(breakdown.sent)))
        values.append((columns.replies, str(breakdown.replies)))
        values.append((columns.rate, format_rate(breakdown.reply_rate)))
    values.append(("note", note))
    return DisplayRow(row_key=row.key, values=tuple(values))


def build_rows(
    sends: Iterable[SendRecord],
    replies: Iterable[ReplyDetection],
    cfg: AnalyticsConfig,
    clock: Clock,
    *,
    granularity: Granularity = Granularity.WEEKLY,
) -> tuple[DisplayRow, ...]:
    """The whole table, rebuilt from scratch. Same input -> same output.

    ``clock`` を受け取るのは「直近コホートか」の判定にだけ使うため
    (:mod:`jobmedley_scout.clock` 以外で実時刻を読まない規約)。時刻は行の値には
    一切入れない -- 生成時刻を1つ混ぜるだけで、毎回全行が差分になる。
    """
    return build_table(sends, replies, cfg, clock, granularity=granularity)[1]


def build_table(
    sends: Iterable[SendRecord],
    replies: Iterable[ReplyDetection],
    cfg: AnalyticsConfig,
    clock: Clock,
    *,
    granularity: Granularity = Granularity.WEEKLY,
) -> tuple[CohortTable, tuple[DisplayRow, ...]]:
    """Both the aggregate (with its unattributed counts) and the display rows.

    :func:`build_rows` が表だけを返すのに対し、こちらは「帳尻の合わない分」も
    返す。描画側が脚注として出せるようにするため -- 黙って落とすと分子だけが
    減った表が出る (:class:`~analytics.aggregate.CohortTable` の説明を参照)。
    """
    now = clock.now()
    records = tuple(sends)
    detections = tuple(replies)
    table = (
        weekly_table(records, detections, cfg, now)
        if granularity is Granularity.WEEKLY
        else monthly_table(records, detections, cfg, now)
    )
    return table, tuple(row_from_cohort(row) for row in table.rows)
