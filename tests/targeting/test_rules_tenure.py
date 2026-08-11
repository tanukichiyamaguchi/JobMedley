"""The masking hole (7.1) and the tenure/age/job-change rules."""

from __future__ import annotations

from jobmedley_scout.models.candidate import Employment
from jobmedley_scout.targeting.determination import Determination
from jobmedley_scout.targeting.filter import apply_targeting
from jobmedley_scout.targeting.rules import (
    job_change_threshold,
    rule_age,
    rule_current_tenure,
    rule_job_change_count,
    rule_longest_tenure,
)
from tests.targeting.factories import (
    QUALIFYING_MEMBERSHIP,
    make_candidate,
    make_passing_candidate,
    make_targeting_config,
)


def test_age_bounds_are_inclusive() -> None:
    cfg = make_targeting_config()
    assert rule_age(make_candidate(age=27), cfg).determination is Determination.MATCH
    assert rule_age(make_candidate(age=42), cfg).determination is Determination.MATCH
    assert rule_age(make_candidate(age=26), cfg).determination is Determination.NO_MATCH
    assert rule_age(make_candidate(age=43), cfg).determination is Determination.NO_MATCH


# --- 7.1 の実事故: 現職不明が別ルールの合格に隠れる ---------------------------


def test_current_tenure_is_undeterminable_when_the_current_job_has_no_tenure() -> None:
    """過去の長期勤続で longest_tenure が合格しても、現職の不明は隠れないこと。"""
    candidate = make_candidate(
        age=35,
        employments=(
            Employment(company="株式会社エー", tenure_years=8.0),
            Employment(company="株式会社ビー", tenure_years=None, is_current=True),
        ),
    )
    cfg = make_targeting_config()
    longest = rule_longest_tenure(candidate, cfg)
    current = rule_current_tenure(candidate, cfg)
    # 過去データでは合格する。これ自体は正しい。
    assert longest.determination is Determination.MATCH
    # だが現職は「不明」であって「合格」ではない。ここが事故の核心。
    assert current.determination is Determination.UNDETERMINABLE


def test_current_tenure_is_undeterminable_when_there_is_no_current_employment() -> None:
    candidate = make_candidate(
        age=35, employments=(Employment(company="株式会社エー", tenure_years=8.0),)
    )
    assert (
        rule_current_tenure(candidate, make_targeting_config()).determination
        is Determination.UNDETERMINABLE
    )


def test_unknown_current_tenure_excludes_an_otherwise_perfect_candidate() -> None:
    """判定不能が最終判定まで生き残り、方針 (exclude) で除外されること。"""
    candidate = make_passing_candidate(
        employments=(
            Employment(company="株式会社エー", tenure_years=8.0),
            Employment(company="株式会社ビー", tenure_years=None, is_current=True),
        )
    )
    result = apply_targeting(candidate, make_targeting_config(), QUALIFYING_MEMBERSHIP)
    assert result.is_target is False
    assert "current_tenure" in result.undeterminable_rules
    assert any(
        reason.startswith("current_tenure") and "判定不能" in reason
        for reason in result.rejection_reasons
    )


def test_current_tenure_below_threshold_is_a_recent_job_change() -> None:
    candidate = make_candidate(
        employments=(Employment(company="株式会社ビー", tenure_years=0.5, is_current=True),)
    )
    outcome = rule_current_tenure(candidate, make_targeting_config())
    assert outcome.determination is Determination.NO_MATCH


# --- longest_tenure -----------------------------------------------------------


def test_longest_tenure_presents_only_the_employers_that_cleared_the_bar() -> None:
    """8.3 対策2: 判定に使った値と提示する値を一致させる。"""
    candidate = make_candidate(
        employments=(
            Employment(company="株式会社エー", tenure_years=8.0),
            Employment(company="株式会社ビー", tenure_years=0.5),
            Employment(company="株式会社シー", tenure_years=1.0, is_current=True),
        )
    )
    outcome = rule_longest_tenure(candidate, make_targeting_config())
    assert outcome.determination is Determination.MATCH
    assert outcome.matched_values == ("株式会社エー",)


def test_longest_tenure_undeterminable_when_known_years_fall_short_but_some_are_missing() -> None:
    candidate = make_candidate(
        employments=(
            Employment(company="株式会社エー", tenure_years=1.0),
            Employment(company="株式会社ビー", tenure_years=None, is_current=True),
        )
    )
    outcome = rule_longest_tenure(candidate, make_targeting_config())
    assert outcome.determination is Determination.UNDETERMINABLE


def test_longest_tenure_no_match_when_every_year_is_known_and_short() -> None:
    candidate = make_candidate(
        employments=(
            Employment(company="株式会社エー", tenure_years=1.0),
            Employment(company="株式会社ビー", tenure_years=2.0, is_current=True),
        )
    )
    outcome = rule_longest_tenure(candidate, make_targeting_config())
    assert outcome.determination is Determination.NO_MATCH


# --- job_change_count ---------------------------------------------------------


def test_job_change_thresholds_by_age_band() -> None:
    cfg = make_targeting_config()
    assert job_change_threshold(29, cfg) == cfg.job_change_threshold_under_30
    assert job_change_threshold(30, cfg) == cfg.job_change_threshold_30s
    assert job_change_threshold(39, cfg) == cfg.job_change_threshold_30s
    assert job_change_threshold(40, cfg) == cfg.job_change_threshold_40_plus


def test_job_change_count_is_undeterminable_without_an_age() -> None:
    candidate = make_candidate(
        employments=(Employment(company="株式会社エー", tenure_years=3.0, is_current=True),)
    )
    assert (
        rule_job_change_count(candidate, make_targeting_config()).determination
        is Determination.UNDETERMINABLE
    )


def test_job_change_count_is_undeterminable_when_an_employer_name_is_missing() -> None:
    # 社名が無いと重複排除できない。多く数えても少なく数えても嘘になる (7.5)。
    candidate = make_candidate(
        age=28,
        employments=(
            Employment(company="株式会社エー", tenure_years=3.0),
            Employment(company=None, tenure_years=2.0, is_current=True),
        ),
    )
    assert (
        rule_job_change_count(candidate, make_targeting_config()).determination
        is Determination.UNDETERMINABLE
    )


def test_job_change_count_excludes_at_the_threshold() -> None:
    # 28歳・閾値3。4社経験 = 転職3回 なので「回数 >= 閾値」で除外。
    candidate = make_candidate(
        age=28,
        employments=(
            Employment(company="株式会社エー", tenure_years=1.0),
            Employment(company="株式会社ビー", tenure_years=1.0),
            Employment(company="株式会社シー", tenure_years=1.0),
            Employment(company="株式会社ディー", tenure_years=1.0, is_current=True),
        ),
    )
    outcome = rule_job_change_count(candidate, make_targeting_config())
    assert outcome.determination is Determination.NO_MATCH
    assert "3回" in outcome.evidence
