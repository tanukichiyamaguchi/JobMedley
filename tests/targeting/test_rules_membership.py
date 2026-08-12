"""The one surviving rule: the membership-status coordinate that is still unknown.

学歴ルールのテストはここにあったが、ルールごと削除した (2026-08-12)。判定ロジックと
その根拠 (6.5 の「大学院」が「大学」に畳まれる話など) は git に残っている。
**弱めたのではなく、対象条件そのものが無くなった。**
"""

from __future__ import annotations

from jobmedley_scout.targeting.determination import Determination
from jobmedley_scout.targeting.rules import rule_membership_status
from tests.targeting.factories import make_candidate, make_targeting_config


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
