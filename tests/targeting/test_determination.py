"""7.1: rules are three-valued, and only one place may collapse that."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobmedley_scout.config.schema import UndeterminablePolicy
from jobmedley_scout.targeting.determination import (
    Determination,
    RuleOutcome,
    matched,
    not_matched,
    resolve_undeterminable,
    undeterminable,
)


def test_match_passes_regardless_of_policy() -> None:
    outcome = matched("age", evidence="範囲内")
    assert resolve_undeterminable(outcome, UndeterminablePolicy.EXCLUDE) is True
    assert resolve_undeterminable(outcome, UndeterminablePolicy.INCLUDE) is True


def test_no_match_fails_regardless_of_policy() -> None:
    outcome = not_matched("age", evidence="範囲外")
    assert resolve_undeterminable(outcome, UndeterminablePolicy.EXCLUDE) is False
    assert resolve_undeterminable(outcome, UndeterminablePolicy.INCLUDE) is False


def test_undeterminable_follows_the_declared_policy() -> None:
    outcome = undeterminable("education", evidence="学歴が未取得")
    # 6.5: 学歴だけ include。取りこぼさない側に倒すという業務判断。
    assert resolve_undeterminable(outcome, UndeterminablePolicy.INCLUDE) is True
    assert resolve_undeterminable(outcome, UndeterminablePolicy.EXCLUDE) is False


def test_matched_values_cannot_ride_on_a_non_match() -> None:
    # 8.3 対策2: 一致していない値を「一致した根拠」として下流へ渡す経路を塞ぐ。
    with pytest.raises(ValidationError):
        RuleOutcome(
            rule_id="longest_tenure",
            determination=Determination.NO_MATCH,
            matched_values=("株式会社エー",),
            evidence="閾値未満",
        )
    with pytest.raises(ValidationError):
        RuleOutcome(
            rule_id="longest_tenure",
            determination=Determination.UNDETERMINABLE,
            matched_values=("株式会社エー",),
            evidence="在籍年数が不明",
        )


def test_outcome_is_frozen() -> None:
    outcome = matched("age", evidence="範囲内", matched_values=("35歳",))
    with pytest.raises(ValidationError):
        outcome.determination = Determination.NO_MATCH  # type: ignore[misc]
