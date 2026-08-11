"""7.5: the current employer appears twice, and the count must not."""

from __future__ import annotations

from jobmedley_scout.models.candidate import Employment
from jobmedley_scout.targeting.dedupe import count_job_changes, dedupe_employers
from jobmedley_scout.targeting.determination import Determination
from jobmedley_scout.targeting.rules import rule_job_change_count
from tests.targeting.factories import make_candidate, make_targeting_config


def test_current_employer_present_in_both_places_is_counted_once() -> None:
    """参照実装が転職回数を1回多く数えた形そのもの。"""
    employers = dedupe_employers("株式会社ビー", ["株式会社エー", "株式会社ビー"])
    assert employers == ("株式会社ビー", "株式会社エー")
    assert count_job_changes("株式会社ビー", ["株式会社エー", "株式会社ビー"]) == 1


def test_naive_count_would_have_been_one_higher() -> None:
    past = ["株式会社エー", "株式会社ビー"]
    naive = len([*past, "株式会社ビー"]) - 1  # 重複排除しない数え方
    assert naive == 2
    assert count_job_changes("株式会社ビー", past) == 1


def test_notation_drift_in_spacing_and_width_is_folded() -> None:
    # 8.6: 比較は共有の正規化関数を通す。全角/半角・空白差は表記のみの差。
    assert count_job_changes("ＡＢＣ 商事", ["ABC商事"]) == 0


def test_display_spelling_is_preserved_for_presentation() -> None:
    # 8.3 対策2: 提示する値は候補者のレジュメの表記のままであること。
    assert dedupe_employers("  株式会社  エー  ", []) == ("株式会社 エー",)


def test_blank_and_missing_names_do_not_become_employers() -> None:
    assert dedupe_employers(None, ["", "   ", None]) == ()
    assert count_job_changes(None, []) == 0


def test_single_employer_is_zero_job_changes() -> None:
    assert count_job_changes("株式会社エー", ["株式会社エー"]) == 0


def test_rule_uses_the_deduped_count() -> None:
    # 28歳・閾値3。実体は3社 (=転職2回) だが、現職が職歴一覧にも入っている。
    # 重複排除しなければ3回となり、閾値に達して誤って除外される。
    candidate = make_candidate(
        age=28,
        employments=(
            Employment(company="株式会社エー", tenure_years=3.0),
            Employment(company="株式会社ビー", tenure_years=3.0),
            Employment(company="株式会社シー", tenure_years=3.0, is_current=True),
        ),
    )
    outcome = rule_job_change_count(candidate, make_targeting_config())
    assert outcome.determination is Determination.MATCH
    assert "2回" in outcome.evidence
