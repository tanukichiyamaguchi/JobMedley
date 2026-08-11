"""8.3 対策3: 4つのルールはそれぞれ実際に起きた誤一致に対応している。"""

from __future__ import annotations

from jobmedley_scout.generation.facts import build_facts
from jobmedley_scout.generation.matching import (
    CommonalityKind,
    MatchRule,
    SenderProfile,
    find_commonalities,
    has_confident_commonality,
)
from tests.generation.helpers import make_candidate, make_matching_config, make_resume


def test_bidirectional_substring_matched_a_different_company() -> None:
    """「トヨタ自動車」(候補者) と「トヨタ自動車直系の販売会社」(マスタ) は
    別の会社である。既定 (順方向のみ) では一致しない。"""
    facts = build_facts(make_candidate(resume=make_resume(current_company="トヨタ自動車")))
    profile = SenderProfile(
        person_id="p-002",
        display_name="鈴木一郎",
        companies=("トヨタ自動車直系の販売会社",),
    )

    assert find_commonalities(facts, profile, make_matching_config()) == ()

    # 双方向を有効にすると拾ってしまう。拾う場合でも断定はさせない。
    loose = find_commonalities(facts, profile, make_matching_config(bidirectional_substring=True))
    assert len(loose) == 1
    assert loose[0].confident is False
    assert loose[0].rule is MatchRule.REVERSE_SUBSTRING


def test_forward_substring_is_still_confident() -> None:
    """順方向 (候補者側がマスタ側を丸ごと含む) は同じ会社と見てよい。"""
    facts = build_facts(make_candidate(resume=make_resume(current_company="トヨタ自動車株式会社")))
    profile = SenderProfile(person_id="p-002", display_name="鈴木一郎", companies=("トヨタ自動車",))

    found = find_commonalities(facts, profile, make_matching_config())

    assert len(found) == 1
    assert found[0].confident is True
    assert found[0].rule is MatchRule.FORWARD_SUBSTRING
    assert found[0].kind is CommonalityKind.COMPANY


def test_short_token_is_excluded_from_suffix_matching() -> None:
    """「営業」(2文字) が12名の「法人営業」に一致し、全員が同じ職種になった。"""
    facts = build_facts(make_candidate(resume=make_resume(current_occupation="営業")))
    profile = SenderProfile(person_id="p-003", display_name="佐藤次郎", occupations=("法人営業",))

    assert find_commonalities(facts, profile, make_matching_config()) == ()

    # 下限を下げれば拾えるが、断定はできない扱いになる。
    loosened = find_commonalities(
        facts, profile, make_matching_config(min_token_length_for_suffix_match=2)
    )
    assert len(loosened) == 1
    assert loosened[0].confident is False
    assert loosened[0].rule is MatchRule.SUFFIX


def test_length_gate_beats_the_bidirectional_flag() -> None:
    """長さで弾いた語を、双方向一致で拾い直させない。"""
    facts = build_facts(make_candidate(resume=make_resume(current_occupation="営業")))
    profile = SenderProfile(person_id="p-003", display_name="佐藤次郎", occupations=("法人営業",))

    found = find_commonalities(facts, profile, make_matching_config(bidirectional_substring=True))

    assert found == ()


def test_generic_industry_term_is_not_evidence_of_a_shared_company() -> None:
    """「サービス業」が「株式会社サービスプロダクト」に一致した。"""
    facts = build_facts(
        make_candidate(resume=make_resume(current_company="株式会社サービス業プロダクト"))
    )
    profile = SenderProfile(person_id="p-004", display_name="高橋三郎", companies=("サービス業",))

    assert find_commonalities(facts, profile, make_matching_config()) == ()

    # 除外リストから外せば一致してしまうことを確認する (ルールが効いている証拠)。
    without_exclusion = find_commonalities(
        facts, profile, make_matching_config(industry_generic_terms=())
    )
    assert len(without_exclusion) == 1
    assert without_exclusion[0].kind is CommonalityKind.COMPANY


def test_job_title_is_not_evidence_of_a_shared_occupation() -> None:
    """「課長」が共通の職種になった。役職は職種ではない。"""
    facts = build_facts(make_candidate(resume=make_resume(current_occupation="営業課長")))
    profile = SenderProfile(person_id="p-005", display_name="伊藤四郎", occupations=("課長",))

    assert find_commonalities(facts, profile, make_matching_config()) == ()

    without_exclusion = find_commonalities(
        facts, profile, make_matching_config(job_title_stopwords=())
    )
    assert len(without_exclusion) == 1
    assert without_exclusion[0].kind is CommonalityKind.OCCUPATION


def test_generic_term_exclusion_is_scoped_to_its_own_kind() -> None:
    """業種の総称は業界の共通点としては正当な根拠である (企業名としてのみ除外)。"""
    facts = build_facts(make_candidate(resume=make_resume(experienced_industries=("介護",))))
    profile = SenderProfile(person_id="p-006", display_name="渡辺五郎", industries=("介護",))

    found = find_commonalities(facts, profile, make_matching_config())

    assert len(found) == 1
    assert found[0].kind is CommonalityKind.INDUSTRY
    assert found[0].confident is True


def test_exact_match_is_confident_and_described_assertively() -> None:
    facts = build_facts(make_candidate(resume=make_resume(school="サンプル大学")))
    profile = SenderProfile(person_id="p-007", display_name="中村六郎", schools=("サンプル大学",))

    found = find_commonalities(facts, profile, make_matching_config())

    assert len(found) == 1
    assert found[0].rule is MatchRule.EXACT
    assert found[0].describe() == "同じ学校（サンプル大学）"
    assert has_confident_commonality(found) is True


def test_loose_match_is_never_described_assertively() -> None:
    """緩い一致は「近い」でしか描けない。断定語が混ざっていないことを確認する。"""
    facts = build_facts(make_candidate(resume=make_resume(current_occupation="福祉士")))
    profile = SenderProfile(person_id="p-008", display_name="小林七郎", occupations=("介護福祉士",))

    found = find_commonalities(facts, profile, make_matching_config())

    assert len(found) == 1
    assert found[0].confident is False
    description = found[0].describe()
    assert description.startswith("近い")
    for assertive in ("同じ", "同様に", "共通点", "同窓", "同郷"):
        assert assertive not in description
    assert has_confident_commonality(found) is False


def test_confident_commonalities_come_first() -> None:
    resume = make_resume(
        current_company="株式会社ケアネット",
        current_occupation="福祉士",
    )
    facts = build_facts(make_candidate(resume=resume))
    profile = SenderProfile(
        person_id="p-009",
        display_name="加藤八郎",
        companies=("株式会社ケアネット",),
        occupations=("介護福祉士",),
    )

    found = find_commonalities(facts, profile, make_matching_config())

    assert [item.confident for item in found] == [True, False]


def test_no_commonalities_when_nothing_is_known() -> None:
    """レジュメが空なら共通点はゼロ。ここで何かが返るなら、それは創作である。"""
    facts = build_facts(make_candidate())
    profile = SenderProfile(
        person_id="p-010",
        display_name="山本九郎",
        companies=("株式会社ケアネット",),
        occupations=("介護福祉士",),
        industries=("介護",),
        schools=("サンプル大学",),
        locations=("東京都",),
    )

    assert find_commonalities(facts, profile, make_matching_config()) == ()


def test_full_width_and_spacing_differences_still_match() -> None:
    facts = build_facts(make_candidate(resume=make_resume(current_company="ＡＢＣ 商事")))
    profile = SenderProfile(person_id="p-011", display_name="森十郎", companies=("ABC商事",))

    found = find_commonalities(facts, profile, make_matching_config())

    assert len(found) == 1
    assert found[0].confident is True
