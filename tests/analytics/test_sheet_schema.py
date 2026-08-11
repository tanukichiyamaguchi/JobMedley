"""11.1: 表示列と入力列の物理分離、および列位置の固定。

参照実装では、自動で取り消した誤検知が **同じ実行の次の段階** で「手入力」として
読み戻され復活した。出力列と入力列が同じ列だったからである。
"""

from __future__ import annotations

from typing import Final

import pytest

from jobmedley_scout.analytics.sheet_schema import (
    ALL_COLUMNS,
    DISPLAY_COLUMNS,
    INPUT_COLUMNS,
    SLOT_COLUMN_KEYS,
    Column,
    ColumnRole,
    assert_columns_disjoint,
    assert_slot_columns_complete,
    blank_input_cell,
    check_columns_disjoint,
    column_index,
    column_order,
    display_keys,
    header_row,
    input_keys,
    is_input_column,
    role_of,
)
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.send_record import SendSlot

#: **この配列は絶対に並べ替えないこと。** グラフは列位置を固定で参照しているので、
#: 途中に1列挿すだけで全グラフが隣の列を描き始める (しかもエラーは出ない)。
#: 新しい列は ALL_COLUMNS の末尾に足し、この配列は末尾に追記するだけにする。
STABLE_PREFIX: Final[tuple[str, ...]] = (
    "cohort",
    "sent",
    "replies",
    "reply_rate_pct",
    "free_sent",
    "free_replies",
    "free_rate_pct",
    "paid_sent",
    "paid_replies",
    "paid_rate_pct",
    "unknown_sent",
    "unknown_replies",
    "unknown_rate_pct",
    "note",
)


def test_display_and_input_columns_are_disjoint() -> None:
    assert_columns_disjoint()

    assert set(display_keys()).isdisjoint(input_keys())
    assert set(DISPLAY_COLUMNS).isdisjoint(INPUT_COLUMNS)


def test_the_check_catches_an_overlap() -> None:
    """検査そのものが効いていることを確かめる。

    「通っている」のか「何も見ていない」のかを区別するため、事故そのものの形
    (既存の表示列と同じキーを入力列として足す) を作って落ちることを見る。
    ``assert`` ではなく ``ConfigError`` が上がること -- python -O で消える形の
    検査ではないこと -- もここで表明する。
    """
    broken = (*ALL_COLUMNS, Column("replies", "返信数(手入力)", ColumnRole.INPUT))

    with pytest.raises(ConfigError, match="11.1"):
        check_columns_disjoint(broken)


def test_the_check_catches_a_duplicate_header() -> None:
    """キーが違ってもヘッダが同じなら、人間はどちらに書くべきか判断できない。"""
    broken = (*ALL_COLUMNS, Column("manual_extra", "手動メモ", ColumnRole.INPUT))

    with pytest.raises(ConfigError, match="ヘッダ"):
        check_columns_disjoint(broken)


def test_the_check_requires_both_roles_to_exist() -> None:
    """入力列を全部消す = 出力列に書かせるということ。11.1 の事故の入り口。"""
    with pytest.raises(ConfigError):
        check_columns_disjoint(DISPLAY_COLUMNS)


def test_column_order_prefix_is_stable() -> None:
    """先頭 N 列の位置は不変。並べ替えは全グラフの参照先をずらす。"""
    order = column_order()

    assert order[: len(STABLE_PREFIX)] == STABLE_PREFIX
    # 入力列は現時点では末尾。新しい列は役割によらず末尾に足すこと。
    assert order[len(STABLE_PREFIX) :] == input_keys()


def test_column_index_is_the_single_source_of_position() -> None:
    assert column_index("cohort") == 0
    assert column_index("note") == len(STABLE_PREFIX) - 1
    with pytest.raises(ConfigError):
        column_index("does_not_exist")


def test_headers_are_unique_and_aligned_with_keys() -> None:
    assert len(header_row()) == len(column_order())
    assert len(set(header_row())) == len(header_row())


def test_roles_are_reported_per_column() -> None:
    assert role_of("cohort") is ColumnRole.DISPLAY
    assert role_of("manual_note") is ColumnRole.INPUT
    assert is_input_column("manual_review") is True
    assert is_input_column("sent") is False
    with pytest.raises(ConfigError):
        role_of("nope")


def test_the_automation_has_exactly_one_value_it_may_write_to_input_columns() -> None:
    assert blank_input_cell() == ""


def test_every_send_slot_has_display_columns() -> None:
    """9.4: 枠を足して列を足し忘れると、合計にだけ現れて内訳から消える。"""
    assert_slot_columns_complete()

    assert set(SLOT_COLUMN_KEYS) == set(SendSlot)
    for columns in SLOT_COLUMN_KEYS.values():
        for key in (columns.sent, columns.replies, columns.rate):
            assert role_of(key) is ColumnRole.DISPLAY


def test_all_columns_is_the_union_of_the_two_roles() -> None:
    assert len(ALL_COLUMNS) == len(DISPLAY_COLUMNS) + len(INPUT_COLUMNS)
    assert DISPLAY_COLUMNS and INPUT_COLUMNS
