"""11.1: 全量書き換えは冪等で、入力列は常に空で書かれる。"""

from __future__ import annotations

import pytest

from jobmedley_scout.analytics.aggregate import RateStatus, ReplyRate
from jobmedley_scout.analytics.cohort import Granularity
from jobmedley_scout.analytics.rewrite import (
    RECENT_NOTE_MONTHLY,
    RECENT_NOTE_WEEKLY,
    DisplayRow,
    build_rows,
    build_table,
    format_rate,
)
from jobmedley_scout.analytics.sheet_schema import (
    ALL_COLUMNS,
    INPUT_COLUMNS,
    ColumnRole,
    column_index,
    column_order,
    input_keys,
)
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.send_record import SendSlot
from tests.analytics.helpers import jst, make_clock, make_config, make_reply, make_send


def test_full_rewrite_is_idempotent_across_two_runs() -> None:
    """同じ入力からは1バイトも変わらない表が出る。差分が出たら時刻が混ざっている。"""
    sends = [
        make_send("c1", jst(2026, 7, 27), slot=SendSlot.FREE, record_id=1),
        make_send("c2", jst(2026, 8, 3), slot=SendSlot.PAID, record_id=2),
        make_send("c3", jst(2026, 8, 3), slot=SendSlot.UNKNOWN, record_id=3),
    ]
    replies = [make_reply("c2")]
    config = make_config()
    clock = make_clock(jst(2026, 8, 11))

    first = build_rows(sends, replies, config, clock)
    second = build_rows(sends, replies, config, clock)

    assert first == second
    assert [row.cells() for row in first] == [row.cells() for row in second]


def test_rewrite_is_insensitive_to_input_ordering() -> None:
    """入力順で表が変わると、DBの取得順が変わっただけで全行が差分になる。"""
    sends = [
        make_send("c1", jst(2026, 8, 3), record_id=1),
        make_send("c2", jst(2026, 8, 3), record_id=2),
        make_send("c3", jst(2026, 8, 4), record_id=3),
    ]
    config = make_config()
    clock = make_clock(jst(2026, 8, 11))

    forward = build_rows(sends, [make_reply("c2")], config, clock)
    backward = build_rows(list(reversed(sends)), [make_reply("c2")], config, clock)

    assert forward == backward


def test_input_columns_are_always_emitted_empty() -> None:
    """自動化が入力列に書いてよい値は空文字だけ (11.1)。"""
    sends = [make_send("c1", jst(2026, 8, 3))]

    rows = build_rows(sends, [make_reply("c1")], make_config(), make_clock(jst(2026, 8, 11)))
    cells = rows[0].cells()

    assert len(cells) == len(column_order())
    for key in input_keys():
        assert cells[column_index(key)] == ""


def test_display_row_has_no_field_for_input_values() -> None:
    """構造で塞ぐ。計算値を入力欄に流し込む経路がコード上に存在しないこと。

    フィールドを足せばこのテストが落ちる。落ちたときに直すべきは、テストでは
    なく設計のほう -- 11.1 の往復ループを再導入する変更である。
    """
    field_names = set(DisplayRow.__dataclass_fields__)

    assert field_names == {"row_key", "values"}
    for name in field_names:
        assert "input" not in name


def test_input_values_cannot_be_smuggled_in_through_display_values() -> None:
    """入力列のキーで値を渡しても、cells() は無視して空文字を書く。"""
    smuggled = DisplayRow(
        row_key="2026-W32",
        values=tuple(
            (column.key, "自動が書いた値")
            for column in ALL_COLUMNS
            if column.role is ColumnRole.DISPLAY
        )
        + tuple((column.key, "混入させたい値") for column in INPUT_COLUMNS),
    )

    cells = smuggled.cells()

    for key in input_keys():
        assert cells[column_index(key)] == ""


def test_a_missing_display_value_is_refused_rather_than_defaulted() -> None:
    """欠けた表示列を空文字で埋めると、集計漏れが「0」に見える。"""
    incomplete = DisplayRow(row_key="2026-W32", values=(("cohort", "2026-W32"),))

    with pytest.raises(ConfigError, match="sent"):
        incomplete.cells()


def test_uncomputable_rate_is_an_empty_cell_not_zero() -> None:
    """空セルはグラフで「点を打たない」。0 は「返信率0%の週」として描かれる。"""
    assert format_rate(ReplyRate(RateStatus.NO_SAMPLE, None, 0, 0)) == ""
    assert format_rate(ReplyRate(RateStatus.SUPPRESSED, None, 3, 10)) == ""
    assert format_rate(ReplyRate(RateStatus.COMPUTED, 0.25, 4, 0)) == "25.0"
    # 本物の 0% はちゃんと 0 と出る。算出不能と区別が付く。
    assert format_rate(ReplyRate(RateStatus.COMPUTED, 0.0, 4, 0)) == "0.0"


def test_recent_cohort_carries_the_annotation() -> None:
    """11.3: 注記が無いと運用者は毎週「今週は悪化した」と誤読する。"""
    sends = [
        make_send("c1", jst(2026, 8, 3), record_id=1),
        make_send("c2", jst(2026, 8, 10), record_id=2),
    ]

    rows = build_rows(sends, [], make_config(), make_clock(jst(2026, 8, 11)))

    assert rows[0].value("note") == ""
    assert rows[1].value("note") == RECENT_NOTE_WEEKLY


def test_monthly_rows_carry_the_monthly_annotation() -> None:
    sends = [make_send("c1", jst(2026, 8, 3))]

    rows = build_rows(
        sends, [], make_config(), make_clock(jst(2026, 8, 11)), granularity=Granularity.MONTHLY
    )

    assert rows[0].row_key == "2026-08"
    assert rows[0].value("note") == RECENT_NOTE_MONTHLY


def test_slot_breakdown_reaches_the_display_columns() -> None:
    sends = [
        make_send("c1", jst(2026, 8, 3), slot=SendSlot.FREE, record_id=1),
        make_send("c2", jst(2026, 8, 3), slot=SendSlot.PAID, record_id=2),
        make_send("c3", jst(2026, 8, 3), slot=SendSlot.UNKNOWN, record_id=3),
    ]

    row = build_rows(sends, [make_reply("c3")], make_config(), make_clock(jst(2026, 8, 11)))[0]

    assert row.value("free_sent") == "1"
    assert row.value("paid_sent") == "1"
    assert row.value("unknown_sent") == "1"
    assert row.value("unknown_replies") == "1"
    assert row.value("sent") == "3"


def test_build_table_also_returns_what_it_could_not_attribute() -> None:
    sends = [make_send("c1", jst(2026, 8, 3))]

    table, rows = build_table(
        sends, [make_reply("ghost")], make_config(), make_clock(jst(2026, 8, 11))
    )

    assert table.unattributed_candidate_ids == ("ghost",)
    assert len(rows) == 1
