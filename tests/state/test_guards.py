"""9.1 / 12.1 / 12.6 -- the safety valves must actually stop the run."""

from __future__ import annotations

import pytest

from jobmedley_scout.errors import KillSwitchEngaged, PermanentError, StateIntegrityError
from jobmedley_scout.state.guards import (
    assert_dry_run_immutable,
    assert_send_history_present,
    check_kill_switch,
)


def test_empty_send_history_stops_a_real_send() -> None:
    """12.1: 送信記録56件の巻き戻り。件名が復元できないので返信も永久に検知不能になる。"""
    with pytest.raises(StateIntegrityError) as excinfo:
        assert_send_history_present(0, enabled=True)
    message = str(excinfo.value)
    assert "状態消失ガード" in message
    assert "12.1" in message  # 事故番号を残す (8.5: 由来の分からないガードは戻される)


def test_a_non_empty_history_passes() -> None:
    # 例外が飛ばないことが合格条件 (返り値は無い)。
    assert_send_history_present(1, enabled=True)


def test_the_guard_does_nothing_when_disabled() -> None:
    """無効化できること自体は仕様 (初回投入)。危険な組み合わせは起動前チェックが弾く (12.6)。"""
    assert_send_history_present(0, enabled=False)


def test_a_negative_history_count_is_rejected() -> None:
    with pytest.raises(ValueError):
        assert_send_history_present(-1, enabled=True)


def test_dry_run_may_not_mutate_state() -> None:
    with pytest.raises(StateIntegrityError) as excinfo:
        assert_dry_run_immutable(True, "send_records への書き込み")
    # どの変更を止めたのかが分からないと調査できない。
    assert "send_records への書き込み" in str(excinfo.value)


def test_a_real_run_may_mutate_state() -> None:
    assert_dry_run_immutable(False, "send_records への書き込み")


def test_the_kill_switch_stops_cleanly() -> None:
    with pytest.raises(KillSwitchEngaged) as excinfo:
        check_kill_switch(True)
    assert "正常停止" in str(excinfo.value)


def test_the_kill_switch_is_not_an_error_condition() -> None:
    """異常終了と混同すると、意図的な停止が監視上ずっと障害として鳴る。"""
    assert not issubclass(KillSwitchEngaged, PermanentError)


def test_no_kill_switch_file_means_carry_on() -> None:
    check_kill_switch(False)


def test_state_integrity_failures_abort_the_run() -> None:
    """状態消失と dry_run 違反は恒久エラー = 非0終了 (原則2)。"""
    assert issubclass(StateIntegrityError, PermanentError)
