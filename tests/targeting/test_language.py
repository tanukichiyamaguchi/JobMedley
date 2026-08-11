"""7.2 + 7.3: foreign-native detection on the language field only."""

from __future__ import annotations

import pytest

from jobmedley_scout.targeting.determination import Determination
from jobmedley_scout.targeting.language import (
    detect_foreign_native,
    detect_foreign_native_detail,
    is_english_dominant,
    japanese_char_ratio,
)
from jobmedley_scout.targeting.rules import rule_foreign_native
from tests.targeting.factories import (
    make_candidate,
    make_foreign_language_config,
    make_targeting_config,
)

CFG = make_foreign_language_config()


# --- 日本人の最頻パターンを除外しないこと -------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "日本語：ネイティブ、英語：ビジネスレベル",
        "日本語：ネイティブ / 英語：日常会話",
        "英語(TOEIC 800)、日本語ネイティブ",
        "母語：日本語　英語：読み書き可",
    ],
)
def test_japanese_native_with_a_foreign_language_is_not_flagged(text: str) -> None:
    """最も多い書き方。共起だけを見ると全滅する (7.3)。"""
    assert detect_foreign_native(text, CFG) is Determination.NO_MATCH


def test_foreign_native_is_flagged_when_strictly_nearer() -> None:
    assert detect_foreign_native("英語：ネイティブ", CFG) is Determination.MATCH
    assert detect_foreign_native("ポルトガル語ネイティブ、日本語は勉強中", CFG) is (
        Determination.MATCH
    )


def test_equal_distance_falls_to_japanese_even_for_a_real_foreign_native() -> None:
    """「英語：ネイティブ、日本語：日常会話」は同点なので除外しない。

    取りこぼし (=送ってしまう) 側の誤りを選んだ意図的な非対称である。7.2 の事故は
    「日本人を誤って除外した」ことであり、そちらの方が高くつく。挙動を固定して
    おかないと、次の読み手が「バグだ」と直してしまう (8.5)。
    """
    assert detect_foreign_native("英語：ネイティブ、日本語：日常会話", CFG) is (
        Determination.NO_MATCH
    )


def test_a_japanese_token_inside_a_foreign_token_does_not_create_a_tie() -> None:
    """「中国語」「韓国語」は日本語トークン「国語」を含む。

    素朴に数えると距離が同点になり、同点は日本語側に倒れるので中国語ネイティブを
    取りこぼす。より長い語の内側に収まる一致は独立した出現として数えない
    (8.3 対策3 の「営業」が「法人営業」に一致した事故と同じ形)。
    """
    assert detect_foreign_native("中国語：ネイティブ", CFG) is Determination.MATCH
    assert detect_foreign_native("韓国語ネイティブ", CFG) is Determination.MATCH


def test_missing_language_field_is_undeterminable() -> None:
    # 6.4: 写像が未確定なら空。空を「日本語話者」と読み替えない。
    assert detect_foreign_native(None, CFG) is Determination.UNDETERMINABLE
    assert detect_foreign_native("   ", CFG) is Determination.UNDETERMINABLE


def test_language_field_without_a_native_marker_is_determinable() -> None:
    assert detect_foreign_native("英語：ビジネスレベル", CFG) is Determination.NO_MATCH


def test_evidence_names_both_sides() -> None:
    detail = detect_foreign_native_detail("日本語：ネイティブ、英語：ビジネス", CFG)
    assert "日本語" in detail.evidence
    assert "英語" in detail.evidence
    assert detail.attribution is not None


# --- 7.2: 適用範囲は語学欄のみ -------------------------------------------------


@pytest.mark.parametrize(
    "summary",
    [
        "ネイティブ広告の運用を5年担当",
        "iOS/Android のネイティブアプリ開発",
        "クラウドネイティブ環境への移行を推進",
    ],
)
def test_compound_words_in_the_summary_never_reach_the_language_rule(summary: str) -> None:
    """職務要約に「ネイティブ」があっても判定に一切入らないこと。

    参照実装はこれで日本人候補者を除外しかけた。複合語の除外リストは **足さない** --
    適用範囲を語学欄に絞ったこと自体が対処である。
    """
    candidate = make_candidate(summary=summary, language_text=None)
    outcome = rule_foreign_native(candidate, make_targeting_config())
    # 語学欄が未取得なので判定不能。要約の「ネイティブ」は評価されていない。
    assert outcome.determination is Determination.UNDETERMINABLE
    assert "語学欄" in outcome.evidence
    # 判定材料として要約が持ち込まれていないこと。
    assert summary not in outcome.evidence


def test_summary_compound_word_with_a_japanese_language_field_still_passes() -> None:
    candidate = make_candidate(
        summary="クラウドネイティブ環境への移行を推進", language_text="日本語：ネイティブ"
    )
    outcome = rule_foreign_native(candidate, make_targeting_config())
    assert outcome.determination is Determination.MATCH


def test_foreign_native_candidate_is_not_a_target() -> None:
    candidate = make_candidate(language_text="英語：ネイティブ")
    outcome = rule_foreign_native(candidate, make_targeting_config())
    # ルールの MATCH は常に「対象である」。該当者はここで NO_MATCH になる。
    assert outcome.determination is Determination.NO_MATCH


# --- 英語優勢 (補助シグナル) ---------------------------------------------------


def test_is_english_dominant_needs_both_conditions() -> None:
    english = (
        "I am a native English speaker from Sydney with ten years of sales experience."
    )
    assert is_english_dominant(english, CFG) is True
    # 文字数は足りても日本語比率が高ければ偽。
    assert is_english_dominant(english + "私は日本語も話します。" * 6, CFG) is False
    # 日本語が無くても文字数が足りなければ偽。
    assert is_english_dominant("English: native", CFG) is False


def test_japanese_ratio_ignores_whitespace() -> None:
    assert japanese_char_ratio("あい うえ") == 1.0
    assert japanese_char_ratio("") == 0.0
