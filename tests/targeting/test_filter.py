"""apply_targeting: aggregation, reporting, and the membership coordinate.

**2026-08-12 の全廃で、走るルールは1本になった。** それ以前、集約の不変条件
(早期returnしない・MATCHした値しか提示しない・include方針が単独で除外しない) は
たまたま8本あったルールの組み合わせで示していた。ルールが1本になった今、同じ
書き方では示せない。

**弱めずに書き直す。** 不変条件は :func:`apply_targeting` と三値
:class:`Determination` の性質であって、特定のルールの性質ではない。だから合成の
ルール集合を差し込んで、性質そのものを検査する。むしろこちらの方が「8本あるから
たまたま通っている」状態から自由になる。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from jobmedley_scout.config.schema import TargetingConfig, UndeterminablePolicy
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.targeting.determination import (
    Determination,
    matched,
    not_matched,
    undeterminable,
)
from jobmedley_scout.targeting.filter import apply_targeting
from jobmedley_scout.targeting.registry import ALL_RULE_IDS
from tests.targeting.factories import (
    DEFAULT_POLICIES,
    QUALIFYING_MEMBERSHIP,
    make_candidate,
    make_passing_candidate,
    make_targeting_config,
)

# --- 合成ルール (集約の不変条件を、実ルールの本数から独立に検査する) ------------

FIRST = "synthetic_first"
SECOND = "synthetic_second"
THIRD = "synthetic_third"


def _always_no_match(_candidate: Candidate, _cfg: TargetingConfig) -> Any:
    return not_matched(FIRST, evidence="合成: 常に不一致")


def _always_matched(_candidate: Candidate, _cfg: TargetingConfig) -> Any:
    return matched(SECOND, evidence="合成: 常に一致", matched_values=("提示してよい値",))


def _always_undeterminable(_candidate: Candidate, _cfg: TargetingConfig) -> Any:
    return undeterminable(THIRD, evidence="合成: 常に判定不能")


@pytest.fixture
def synthetic_rules(monkeypatch: pytest.MonkeyPatch) -> Iterator[TargetingConfig]:
    """Three rules whose verdicts are fixed, so aggregation can be observed alone."""
    monkeypatch.setattr(
        "jobmedley_scout.targeting.filter.ALL_RULES",
        (
            (FIRST, _always_no_match),
            (SECOND, _always_matched),
            (THIRD, _always_undeterminable),
        ),
    )
    monkeypatch.setattr(
        "jobmedley_scout.targeting.filter.assert_policies_complete", lambda _cfg: None
    )
    yield make_targeting_config(
        undeterminable_policy={
            FIRST: UndeterminablePolicy.EXCLUDE,
            SECOND: UndeterminablePolicy.EXCLUDE,
            THIRD: UndeterminablePolicy.INCLUDE,
        }
    )


def test_every_rule_is_evaluated_even_after_one_fails(synthetic_rules: TargetingConfig) -> None:
    """早期 return すると「最初に落ちた理由」しか残らない。

    閾値を緩めたときの見積もりができなくなるので、1本落ちても最後まで回す。
    """
    result = apply_targeting(make_candidate(), synthetic_rules)

    assert tuple(outcome.rule_id for outcome in result.outcomes) == (FIRST, SECOND, THIRD)
    assert result.is_target is False


def test_matched_values_carry_only_values_from_matching_rules(
    synthetic_rules: TargetingConfig,
) -> None:
    """8.3 対策2: 判定に使った値と提示する値を一致させる。

    MATCH 以外の結果に値を持たせること自体が :class:`RuleOutcome` の側で
    構築不能になっているが、集約側でも混入しないことを押さえる。
    """
    result = apply_targeting(make_candidate(), synthetic_rules)

    assert result.matched_values == ("提示してよい値",)


def test_an_include_policy_rule_does_not_reject_on_its_own(
    synthetic_rules: TargetingConfig,
) -> None:
    """include 方針の判定不能は、それ単独では除外理由にならない (7.1)。

    以前は学歴ルールで示していた (6.5: 大学卒以上を取りこぼさない)。ルールごと
    削除したので、方針そのものの振る舞いとして書き直してある。
    """
    result = apply_targeting(make_candidate(), synthetic_rules)

    assert THIRD in result.undeterminable_rules
    assert not any(THIRD in reason for reason in result.rejection_reasons)


def test_a_match_never_conceals_another_rule_being_undeterminable(
    synthetic_rules: TargetingConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7.1 の穴。**一致した判定が、別の判定不能を覆い隠してはならない。**

    以前は勤続年数の2ルールで示していた (最長勤続の MATCH が現職在籍の
    判定不能を隠す形)。両方削除したので、性質として書き直した -- 隠蔽が起きるのは
    集約の書き方の問題であって、勤続年数固有の問題ではなかった。
    """
    monkeypatch.setattr(
        "jobmedley_scout.targeting.filter.ALL_RULES",
        ((SECOND, _always_matched), (THIRD, _always_undeterminable)),
    )
    cfg = make_targeting_config(
        undeterminable_policy={
            SECOND: UndeterminablePolicy.EXCLUDE,
            THIRD: UndeterminablePolicy.EXCLUDE,
        }
    )

    result = apply_targeting(make_candidate(), cfg)

    # MATCH が先に出ていても、後続の判定不能は握りつぶされない。
    assert THIRD in result.undeterminable_rules
    assert result.is_target is False
    assert any(THIRD in reason for reason in result.rejection_reasons)


# --- 実際に走るルール ---------------------------------------------------------


def test_a_fully_known_candidate_is_a_target() -> None:
    result = apply_targeting(
        make_passing_candidate(), make_targeting_config(), QUALIFYING_MEMBERSHIP
    )
    assert result.is_target is True
    assert result.rejection_reasons == ()
    assert result.undeterminable_rules == ()


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


def test_apply_targeting_refuses_an_incomplete_policy_map() -> None:
    policies = {k: v for k, v in DEFAULT_POLICIES.items() if k != "membership_status"}
    with pytest.raises(ConfigError):
        apply_targeting(make_candidate(), make_targeting_config(undeterminable_policy=policies))


def test_outcomes_expose_the_three_valued_verdict_not_a_bool() -> None:
    result = apply_targeting(make_candidate(), make_targeting_config())
    for outcome in result.outcomes:
        assert isinstance(outcome.determination, Determination)


def test_the_shipped_rule_set_is_what_the_report_names() -> None:
    """走っているルールと、レポートに出るルールが一致すること。"""
    result = apply_targeting(make_candidate(), make_targeting_config())

    assert tuple(outcome.rule_id for outcome in result.outcomes) == ALL_RULE_IDS
