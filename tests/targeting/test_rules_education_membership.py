"""6.5 (education, the include side) and the unconfirmed membership coordinate."""

from __future__ import annotations

import pytest

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.candidate import Education
from jobmedley_scout.models.enums import EducationLevel
from jobmedley_scout.targeting.determination import Determination
from jobmedley_scout.targeting.rules import (
    map_education_level,
    rule_education,
    rule_membership_status,
)
from tests.targeting.factories import make_candidate, make_targeting_config


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("大学卒", EducationLevel.UNIVERSITY),
        ("大学院卒", EducationLevel.GRADUATE),
        ("修士", EducationLevel.GRADUATE),
        ("博士課程修了", EducationLevel.DOCTORATE),
        ("短期大学卒", EducationLevel.JUNIOR_COLLEGE),
        ("高等専門学校卒", EducationLevel.TECHNICAL_COLLEGE),
        ("専門学校卒", EducationLevel.VOCATIONAL),
        ("高校卒", EducationLevel.HIGH_SCHOOL),
        ("university", EducationLevel.UNIVERSITY),
        ("見たことのない区分", EducationLevel.UNKNOWN),
        (None, EducationLevel.UNKNOWN),
    ],
)
def test_education_keyword_order_is_the_spec(raw: str | None, expected: EducationLevel) -> None:
    """「大学院」は「大学」を含む。上位学歴から判定しないと畳まれる (6.5)。"""
    assert map_education_level(raw) is expected


def test_unknown_raw_level_is_undeterminable_not_a_rejection() -> None:
    # 参照実装はマップに無い値を判定不能→除外に落とし、対象者を取りこぼした。
    candidate = make_candidate(educations=(Education(school="A大学", raw_level="謎の区分"),))
    outcome = rule_education(candidate, make_targeting_config())
    assert outcome.determination is Determination.UNDETERMINABLE
    # 6.5: 生値を残す。マップに無い値が来ていることが読み取れないと誰も気づけない。
    assert "謎の区分" in outcome.evidence


def test_highest_level_wins_when_several_educations_exist() -> None:
    candidate = make_candidate(
        educations=(
            Education(school="◯◯高等学校", raw_level="高校卒"),
            Education(school="東京大学", raw_level="大学卒"),
        )
    )
    outcome = rule_education(candidate, make_targeting_config())
    assert outcome.determination is Determination.MATCH
    # 8.3 対策2: 条件を満たした学校だけを提示する。高校は根拠ではない。
    assert outcome.matched_values == ("東京大学",)


def test_below_minimum_is_a_clean_no_match() -> None:
    candidate = make_candidate(educations=(Education(school="◯◯高校", raw_level="高校卒"),))
    assert (
        rule_education(candidate, make_targeting_config()).determination is Determination.NO_MATCH
    )


def test_unknown_minimum_education_in_config_is_rejected() -> None:
    candidate = make_candidate(educations=(Education(raw_level="大学卒"),))
    with pytest.raises(ConfigError):
        rule_education(candidate, make_targeting_config(minimum_education="だいがく"))
    with pytest.raises(ConfigError):
        rule_education(candidate, make_targeting_config(minimum_education="unknown"))


# --- 会員ステータス (未確定の媒体座標) ----------------------------------------


def test_membership_without_qualifying_values_is_undeterminable() -> None:
    """対象値を推測して比較しない。既定値を置かないことで構造的に防ぐ。"""
    candidate = make_candidate(membership_status="スカウト受付中")
    outcome = rule_membership_status(candidate, make_targeting_config())
    assert outcome.determination is Determination.UNDETERMINABLE
    assert "未確定" in outcome.evidence


def test_membership_matches_only_declared_values() -> None:
    cfg = make_targeting_config()
    qualifying = ("スカウト受付中",)
    assert (
        rule_membership_status(
            make_candidate(membership_status="スカウト受付中"), cfg, qualifying=qualifying
        ).determination
        is Determination.MATCH
    )
    assert (
        rule_membership_status(
            make_candidate(membership_status="退会済み"), cfg, qualifying=qualifying
        ).determination
        is Determination.NO_MATCH
    )
    assert (
        rule_membership_status(make_candidate(), cfg, qualifying=qualifying).determination
        is Determination.UNDETERMINABLE
    )


def test_membership_presents_the_raw_status_it_matched() -> None:
    outcome = rule_membership_status(
        make_candidate(membership_status="スカウト受付中"),
        make_targeting_config(),
        qualifying=("スカウト受付中",),
    )
    assert outcome.matched_values == ("スカウト受付中",)
