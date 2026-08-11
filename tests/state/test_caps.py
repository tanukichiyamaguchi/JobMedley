"""9.7 -- one number per cost unit, and truncation is never silent."""

from __future__ import annotations

import pytest

from jobmedley_scout.models.send_record import SendSlot
from jobmedley_scout.state.caps import (
    allocate,
    apply_ingest_cap,
    priority_order,
    remaining_capacity,
)


def test_remaining_capacity_is_per_slot() -> None:
    remaining = remaining_capacity(
        {SendSlot.PAID: 10, SendSlot.FREE: 1},
        {SendSlot.PAID: 10, SendSlot.FREE: 5},
    )
    # 有料枠を使い切っても無料枠の残量は減らない。ここが 9.7 の核心。
    assert remaining[SendSlot.PAID] == 0
    assert remaining[SendSlot.FREE] == 4


def test_remaining_capacity_reports_every_slot() -> None:
    """9.4 の恒等式を保つため、0件でも枠を省略しない。"""
    remaining = remaining_capacity({}, {SendSlot.FREE: 3})
    assert set(remaining) == set(SendSlot)
    # 上限が設定されていない枠は「無制限」ではなく0 (安全側は送らない方)。
    assert remaining[SendSlot.UNKNOWN] == 0
    assert remaining[SendSlot.PAID] == 0


def test_overshooting_the_cap_never_goes_negative() -> None:
    remaining = remaining_capacity({SendSlot.PAID: 12}, {SendSlot.PAID: 10})
    assert remaining[SendSlot.PAID] == 0


def test_negative_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        remaining_capacity({}, {SendSlot.FREE: -1})
    with pytest.raises(ValueError):
        remaining_capacity({SendSlot.FREE: -1}, {SendSlot.FREE: 1})


def test_the_paid_cap_does_not_starve_the_free_slot() -> None:
    """参照実装の事故そのもの: 無料枠の処理が有料枠の上限に食われて未処理になった。"""
    allocation = allocate(
        {
            SendSlot.PAID: ["P1", "P2", "P3", "P4", "P5"],
            SendSlot.FREE: ["F1", "F2", "F3"],
        },
        {SendSlot.PAID: 2, SendSlot.FREE: 3},
    )
    assert allocation.granted(SendSlot.PAID) == ("P1", "P2")
    # 共有プールなら有料枠が食い尽くして無料枠は0件になるはずのところ、満額付与される。
    assert allocation.granted(SendSlot.FREE) == ("F1", "F2", "F3")
    assert allocation.truncated_count(SendSlot.FREE) == 0
    assert allocation.truncated_count(SendSlot.PAID) == 3


def test_a_slot_with_no_candidates_does_not_affect_the_others() -> None:
    allocation = allocate(
        {SendSlot.PAID: [], SendSlot.FREE: ["F1", "F2"]},
        {SendSlot.PAID: 5, SendSlot.FREE: 5},
    )
    assert allocation.granted(SendSlot.FREE) == ("F1", "F2")
    assert allocation.granted_count(SendSlot.PAID) == 0


def test_already_sent_counts_consume_only_their_own_slot() -> None:
    allocation = allocate(
        {SendSlot.PAID: ["P1", "P2"], SendSlot.FREE: ["F1", "F2"]},
        {SendSlot.PAID: 2, SendSlot.FREE: 2},
        sent_by_slot={SendSlot.PAID: 2},
    )
    assert allocation.granted(SendSlot.PAID) == ()
    assert allocation.granted(SendSlot.FREE) == ("F1", "F2")


def test_truncation_is_always_reported_with_counts_and_advice() -> None:
    allocation = allocate({SendSlot.FREE: ["F1", "F2", "F3"]}, {SendSlot.FREE: 1})
    assert allocation.was_truncated is True
    assert allocation.total_truncated == 2
    described = allocation.describe()
    assert "保留2件" in described
    assert "次回実行に回る" in described
    # 取り込み上限と混同させないことまで書いてあること (9.7)。
    assert "ingest_cap" in described


def test_nothing_truncated_means_no_advice_line() -> None:
    allocation = allocate({SendSlot.FREE: ["F1"]}, {SendSlot.FREE: 5})
    assert allocation.was_truncated is False
    assert "保留" not in allocation.describe()


def test_totals_add_up_across_slots() -> None:
    allocation = allocate(
        {SendSlot.FREE: ["F1", "F2"], SendSlot.PAID: ["P1"], SendSlot.UNKNOWN: ["U1"]},
        {SendSlot.FREE: 1, SendSlot.PAID: 1, SendSlot.UNKNOWN: 0},
    )
    assert allocation.total_granted == 2
    assert allocation.total_truncated == 2
    assert allocation.granted(SendSlot.UNKNOWN) == ()


def test_higher_priority_segment_is_processed_first() -> None:
    assert priority_order([SendSlot.UNKNOWN, SendSlot.PAID, SendSlot.FREE]) == (
        SendSlot.FREE,
        SendSlot.PAID,
        SendSlot.UNKNOWN,
    )
    assert priority_order([SendSlot.PAID, SendSlot.PAID]) == (SendSlot.PAID,)


def test_ordered_items_follow_the_priority_order() -> None:
    allocation = allocate(
        {SendSlot.PAID: ["P1"], SendSlot.FREE: ["F1"]},
        {SendSlot.PAID: 5, SendSlot.FREE: 5},
    )
    # 優先度は処理順にだけ効く。付与数は枠ごとに独立に決まっている。
    assert allocation.ordered_items() == ("F1", "P1")
    assert allocation.ordered_slots == (SendSlot.FREE, SendSlot.PAID)


def test_ingest_cap_is_a_different_number_from_the_send_cap() -> None:
    """同じ数で兼用すると「取り込まれなかったから送られない」が静かに起きる (9.7)。"""
    found = [f"C-{i}" for i in range(1, 11)]
    ingest = apply_ingest_cap(found, 4)
    assert ingest.kept == ("C-1", "C-2", "C-3", "C-4")
    assert ingest.total == 10
    assert ingest.truncated == 6

    # 取り込み4件に対して送信上限が10件でも、送信側は4件しか見られない。
    allocation = allocate({SendSlot.FREE: list(ingest.kept)}, {SendSlot.FREE: 10})
    assert allocation.granted_count(SendSlot.FREE) == 4
    assert allocation.was_truncated is False  # 送信側の上限には当たっていない
    # 送信側のログだけを見ると「対象が少なかった」としか見えない。だから取り込み側が報告する。
    assert "保留6件" in ingest.describe()
    assert "safety.ingest_cap" in ingest.describe()


def test_ingest_within_the_cap_is_not_truncated() -> None:
    ingest = apply_ingest_cap(["C-1", "C-2"], 5)
    assert ingest.kept == ("C-1", "C-2")
    assert ingest.truncated == 0
    assert ingest.was_truncated is False


def test_a_zero_ingest_cap_holds_everything_back_loudly() -> None:
    ingest = apply_ingest_cap(["C-1", "C-2"], 0)
    assert ingest.kept == ()
    assert ingest.truncated == 2
    assert ingest.was_truncated is True


def test_a_negative_ingest_cap_is_rejected() -> None:
    with pytest.raises(ValueError):
        apply_ingest_cap(["C-1"], -1)
