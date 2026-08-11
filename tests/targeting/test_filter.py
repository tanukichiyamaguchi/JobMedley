"""apply_targeting: aggregation, reporting, and the membership coordinate."""

from __future__ import annotations

import pytest

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.candidate import Education, Employment
from jobmedley_scout.targeting.determination import Determination
from jobmedley_scout.targeting.filter import apply_targeting
from jobmedley_scout.targeting.registry import ALL_RULE_IDS
from tests.targeting.factories import (
    DEFAULT_POLICIES,
    QUALIFYING_MEMBERSHIP,
    make_candidate,
    make_passing_candidate,
    make_targeting_config,
)


def test_a_fully_known_candidate_is_a_target() -> None:
    result = apply_targeting(
        make_passing_candidate(), make_targeting_config(), QUALIFYING_MEMBERSHIP
    )
    assert result.is_target is True
    assert result.rejection_reasons == ()
    assert result.undeterminable_rules == ()


def test_every_rule_is_evaluated_even_after_one_fails() -> None:
    # 早期 return すると「最初に落ちた理由」しか残らず、閾値を緩めたときの
    # 見積もりができなくなる。
    candidate = make_passing_candidate(age=19, language_text="英語：ネイティブ")
    result = apply_targeting(candidate, make_targeting_config(), QUALIFYING_MEMBERSHIP)
    assert tuple(outcome.rule_id for outcome in result.outcomes) == ALL_RULE_IDS
    reasons = "\n".join(result.rejection_reasons)
    assert "age" in reasons
    assert "foreign_native" in reasons


def test_matched_values_carry_only_values_from_matching_rules() -> None:
    """8.3 対策2: 判定に使った値と提示する値を一致させる。"""
    candidate = make_passing_candidate(
        employments=(
            Employment(company="株式会社エー", tenure_years=8.0),
            Employment(company="株式会社ビー", tenure_years=0.4),
            Employment(company="株式会社シー", tenure_years=3.0, is_current=True),
        ),
    )
    result = apply_targeting(candidate, make_targeting_config(), QUALIFYING_MEMBERSHIP)
    # 0.4年の「株式会社ビー」は何の条件も満たしていないので提示されない。
    assert "株式会社ビー" not in result.matched_values
    assert "株式会社エー" in result.matched_values
    assert "株式会社シー" in result.matched_values


def test_membership_qualifying_values_default_to_undeterminable() -> None:
    """会員ステータスの対象値は未確定の媒体座標。推測して比較しない。"""
    result = apply_targeting(make_passing_candidate(), make_targeting_config())
    assert "membership_status" in result.undeterminable_rules
    # 方針は exclude なので、座標が未確定のうちは対象にならない。
    assert result.is_target is False


def test_membership_qualifying_values_are_compared_normalized() -> None:
    candidate = make_passing_candidate(membership_status=" スカウト受付中 ")
    result = apply_targeting(candidate, make_targeting_config(), QUALIFYING_MEMBERSHIP)
    assert result.is_target is True


def test_non_qualifying_membership_is_rejected_with_a_reason() -> None:
    candidate = make_passing_candidate(membership_status="退会済み")
    result = apply_targeting(candidate, make_targeting_config(), QUALIFYING_MEMBERSHIP)
    assert result.is_target is False
    assert any("membership_status" in reason for reason in result.rejection_reasons)


def test_education_undeterminable_does_not_reject_by_itself() -> None:
    # 6.5: 大学卒以上を取りこぼさない。学歴だけ include 方針。
    candidate = make_passing_candidate(
        educations=(Education(school="東京大学", raw_level="不明な区分"),)
    )
    result = apply_targeting(candidate, make_targeting_config(), QUALIFYING_MEMBERSHIP)
    assert "education" in result.undeterminable_rules
    assert result.is_target is True
    assert not any("education" in reason for reason in result.rejection_reasons)


def test_apply_targeting_refuses_an_incomplete_policy_map() -> None:
    policies = {k: v for k, v in DEFAULT_POLICIES.items() if k != "age"}
    with pytest.raises(ConfigError):
        apply_targeting(make_candidate(), make_targeting_config(undeterminable_policy=policies))


def test_outcomes_expose_the_three_valued_verdict_not_a_bool() -> None:
    result = apply_targeting(make_candidate(), make_targeting_config())
    for outcome in result.outcomes:
        assert isinstance(outcome.determination, Determination)
