"""9.6 -- the bug that produced no error at all, only a count.

固定順「送信の古い順に上限件数」では、対象が上限を超えた瞬間から同じ最古のN件
だけを毎回再訪し、返信が最も来やすい直近の送信者を永久に見逃していた。
中心となる表明は :func:`test_five_items_with_limit_two_are_all_visited_in_three_runs`
-- 上限で切られた分が次回に回ること (飢餓が起きないこと) そのもの。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobmedley_scout.state.rotation import (
    RotationEntry,
    RotationResult,
    advance_batch,
    advance_cursor,
    order_for_rotation,
    select_batch,
)

RUN1 = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
RUN2 = RUN1 + timedelta(days=1)
RUN3 = RUN1 + timedelta(days=2)


def _at(days_ago: float) -> datetime:
    return RUN1 - timedelta(days=days_ago)


def test_never_checked_entries_come_first() -> None:
    entries = (
        RotationEntry("C-old", _at(30)),
        RotationEntry("C-new", _at(1)),
        RotationEntry("C-never", None),
    )
    assert [e.candidate_id for e in order_for_rotation(entries)] == ["C-never", "C-old", "C-new"]


def test_after_the_never_checked_the_order_is_longest_ago_first() -> None:
    entries = (
        RotationEntry("C-b", _at(2)),
        RotationEntry("C-a", _at(10)),
        RotationEntry("C-c", _at(5)),
    )
    assert [e.candidate_id for e in order_for_rotation(entries)] == ["C-a", "C-c", "C-b"]


def test_ties_are_broken_deterministically_by_id() -> None:
    """順序が非決定的だと「毎回同じ対象が落ちる」状況を再現できず、9.6 を押さえられない。"""
    same = _at(3)
    entries = (RotationEntry("C-2", same), RotationEntry("C-1", same), RotationEntry("C-3", None))
    assert [e.candidate_id for e in order_for_rotation(entries)] == ["C-3", "C-1", "C-2"]


def test_five_items_with_limit_two_are_all_visited_in_three_runs() -> None:
    """飢餓が起きないことの網羅的な表明 (9.6 の本体)。

    上限は1回の実行時間を守るためのものであって対象を絞るためではない。したがって
    上限で切られた分は **次回に回り**、有限回の実行で必ず全件が一巡する。
    """
    entries = tuple(RotationEntry(f"C-{i}") for i in range(1, 6))
    visited: list[str] = []

    for now in (RUN1, RUN2, RUN3):
        batch = select_batch(entries, limit=2)
        assert batch.total_candidates == 5
        visited.extend(batch.selected)
        entries = advance_batch(entries, batch.selected, RotationResult.PROCESSED, now)

    assert len(visited) == 6  # 3回 × 上限2件
    assert set(visited) == {f"C-{i}" for i in range(1, 6)}  # 全件が少なくとも1回は訪問された
    assert all(not entry.never_processed for entry in entries)


def test_the_fixed_order_that_caused_the_incident_would_starve_the_rest() -> None:
    """対照実験: カーソルを持たない「古い順に上限件数」だと同じ2件しか見ない。"""
    # 送信が古い順 = ID順 になるように作ってある (C-1 が最古)。固定順の抽出はカーソルを
    # 更新しないので、何回実行しても同じ先頭2件を返し続ける。
    entries = tuple(RotationEntry(f"C-{i}", _at(10 - i)) for i in range(1, 6))
    fixed_order = tuple(sorted(entry.candidate_id for entry in entries))
    fixed_order_visits = [fixed_order[:2] for _ in range(3)]
    assert set(fixed_order_visits) == {("C-1", "C-2")}  # 3回とも同じ2件 = 残り3件は永久に未訪問

    visited: set[str] = set()
    rotating = entries
    for now in (RUN1, RUN2, RUN3):
        batch = select_batch(rotating, limit=2)
        visited.update(batch.selected)
        rotating = advance_batch(rotating, batch.selected, RotationResult.PROCESSED, now)
    assert visited == {f"C-{i}" for i in range(1, 6)}


def test_recently_checked_entries_are_not_revisited_before_the_rest() -> None:
    entries = (
        RotationEntry("C-just-now", RUN1),
        RotationEntry("C-a-week-ago", _at(7)),
        RotationEntry("C-never", None),
    )
    batch = select_batch(entries, limit=2)
    assert batch.selected == ("C-never", "C-a-week-ago")


def test_the_batch_reports_the_counts_that_reveal_the_bug() -> None:
    entries = tuple(RotationEntry(f"C-{i}") for i in range(1, 6))
    batch = select_batch(entries, limit=2)
    assert batch.total_candidates == 5
    assert batch.truncated == 3
    assert batch.never_processed == 5
    assert batch.was_truncated is True
    # 12.8: この件数行が出ていなければ 9.6 は誰にも気づかれない。
    assert "対象5件・処理2件" in batch.describe()
    assert "次回実行に回る" in batch.describe()


def test_a_batch_within_the_limit_is_not_truncated() -> None:
    batch = select_batch((RotationEntry("C-1"), RotationEntry("C-2")), limit=5)
    assert batch.selected == ("C-1", "C-2")
    assert batch.truncated == 0
    assert batch.was_truncated is False


def test_an_empty_scope_is_a_valid_empty_batch() -> None:
    batch = select_batch((), limit=3)
    assert batch.selected == ()
    assert batch.total_candidates == 0
    assert batch.truncated == 0


def test_a_zero_limit_is_rejected() -> None:
    """上限0はバッチを永久に空にする。設定ミスを黙って受け入れない (7.6)。"""
    with pytest.raises(ValueError):
        select_batch((RotationEntry("C-1"),), limit=0)


def test_the_cursor_advances_on_error() -> None:
    """成功時だけ前進させると、失敗する対象が先頭に居座って後続が永久に処理されない。"""
    entry = RotationEntry("C-broken")
    advanced = advance_cursor(entry, RotationResult.ERROR, RUN1)
    assert advanced.last_processed_at == RUN1
    assert advanced.last_result is RotationResult.ERROR
    assert advanced.never_processed is False
    assert entry.last_processed_at is None  # 元の値は不変


def test_the_cursor_advances_on_skip() -> None:
    advanced = advance_cursor(RotationEntry("C-skipped"), RotationResult.SKIPPED, RUN1)
    assert advanced.last_processed_at == RUN1
    assert advanced.last_result is RotationResult.SKIPPED


def test_an_erroring_item_does_not_jam_the_batch() -> None:
    """同じ対象で詰まらないこと。9.6 の「同じ最古N件を再訪し続ける」形の再発防止。"""
    entries: tuple[RotationEntry, ...] = (
        RotationEntry("C-broken"),
        RotationEntry("C-ok"),
        RotationEntry("C-waiting"),
    )
    first = select_batch(entries, limit=1)
    assert first.selected == ("C-broken",)

    entries = advance_batch(entries, first.selected, RotationResult.ERROR, RUN1)
    second = select_batch(entries, limit=1)
    assert second.selected == ("C-ok",)  # エラーでも前進したので次へ進んだ

    entries = advance_batch(entries, second.selected, RotationResult.ERROR, RUN2)
    assert select_batch(entries, limit=1).selected == ("C-waiting",)


def test_advance_batch_leaves_untouched_entries_alone() -> None:
    entries = (RotationEntry("C-1"), RotationEntry("C-2"))
    advanced = advance_batch(entries, ("C-1",), RotationResult.PROCESSED, RUN1)
    assert advanced[0].last_processed_at == RUN1
    assert advanced[1].last_processed_at is None


def test_naive_datetimes_are_rejected() -> None:
    naive = datetime(2026, 4, 1, 0, 0)  # 意図的に naive
    with pytest.raises(ValueError):
        advance_cursor(RotationEntry("C-1"), RotationResult.PROCESSED, naive)
    with pytest.raises(ValueError):
        order_for_rotation((RotationEntry("C-1", naive),))
