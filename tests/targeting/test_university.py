"""7.4: script does not equal origin."""

from __future__ import annotations

from jobmedley_scout.models.candidate import Education
from jobmedley_scout.targeting.determination import Determination
from jobmedley_scout.targeting.rules import rule_domestic_university
from jobmedley_scout.targeting.university import (
    classify_university,
    is_overseas_university,
    strip_school_suffixes,
)
from tests.targeting.factories import make_candidate, make_targeting_config

ALLOWLIST = make_targeting_config().domestic_katakana_universities


def test_stanford_written_in_japanese_is_overseas() -> None:
    """参照実装はこれを国内と判定した。日本語表記であることは出自ではない。"""
    assert is_overseas_university("スタンフォード大学", ALLOWLIST) is Determination.MATCH


def test_allowlisted_katakana_school_is_domestic() -> None:
    # 核までカタカナの国内校。コード定数ではなく設定で吸収する。
    assert is_overseas_university("サイバー大学", ALLOWLIST) is Determination.NO_MATCH
    assert is_overseas_university("デジタルハリウッド大学", ALLOWLIST) is Determination.NO_MATCH


def test_allowlist_is_not_a_code_constant() -> None:
    # 許可リストを外すと同じ学校が海外判定になる = リストは設定由来である証拠。
    assert is_overseas_university("サイバー大学", ()) is Determination.MATCH


def test_kanji_school_is_domestic() -> None:
    assert is_overseas_university("東京大学", ALLOWLIST) is Determination.NO_MATCH
    assert is_overseas_university("ノートルダム清心女子大学", ALLOWLIST) is (Determination.NO_MATCH)


def test_unknown_or_empty_is_undeterminable() -> None:
    assert is_overseas_university(None, ALLOWLIST) is Determination.UNDETERMINABLE
    assert is_overseas_university("   ", ALLOWLIST) is Determination.UNDETERMINABLE
    assert is_overseas_university("大学", ALLOWLIST) is Determination.UNDETERMINABLE


def test_latin_script_is_not_folded_into_domestic() -> None:
    # "Stanford University" を国内に畳むと 7.4 の事故を別の文字種で再演する。
    assert is_overseas_university("Stanford University", ALLOWLIST) is (
        Determination.UNDETERMINABLE
    )


# --- 接尾辞の除去順序 (順序そのものが仕様) ------------------------------------


def test_graduate_suffix_is_stripped_before_university() -> None:
    """「大学」を先に落とすと「◯◯大学院」の核が「◯◯院」になる。"""
    assert strip_school_suffixes("スタンフォード大学院") == "スタンフォード"
    assert strip_school_suffixes("東京大学大学院") == "東京"
    assert strip_school_suffixes("北陸先端科学技術大学院大学") == "北陸先端科学技術"
    assert strip_school_suffixes("防衛大学校") == "防衛"


def test_stripping_order_keeps_katakana_core_intact() -> None:
    # 順序が崩れると核に「院」(漢字) が残り、海外校が国内と判定される。
    assert classify_university("スタンフォード大学院", ALLOWLIST).core == "スタンフォード"
    assert is_overseas_university("スタンフォード大学院", ALLOWLIST) is Determination.MATCH


# --- ルールとしての振る舞い ---------------------------------------------------


def test_rule_excludes_a_candidate_with_an_overseas_school() -> None:
    candidate = make_candidate(
        educations=(Education(school="スタンフォード大学", raw_level="大学卒"),)
    )
    outcome = rule_domestic_university(candidate, make_targeting_config())
    assert outcome.determination is Determination.NO_MATCH


def test_rule_presents_only_the_domestic_schools_it_matched() -> None:
    candidate = make_candidate(
        educations=(
            Education(school="東京大学", raw_level="大学卒"),
            Education(school=None, raw_level="高校卒"),
        )
    )
    outcome = rule_domestic_university(candidate, make_targeting_config())
    assert outcome.determination is Determination.MATCH
    assert outcome.matched_values == ("東京大学",)


def test_rule_is_undeterminable_when_no_school_can_be_classified() -> None:
    candidate = make_candidate(educations=(Education(school=None, raw_level="大学卒"),))
    outcome = rule_domestic_university(candidate, make_targeting_config())
    assert outcome.determination is Determination.UNDETERMINABLE
