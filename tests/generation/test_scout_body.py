"""運用者のプロンプトが定めた決まりを、検査で固定する。

**モデルの自己チェックを信用しない。** プロンプトの STEP4 に「確認し、違反が
あれば書き直してから出力する」と書いてあっても、書いてあることは守られること
ではない。守らせるのは検査である (8.5 と同じ考え方)。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.generation.scout_body import (
    APPLY_BUTTON,
    HEADLINE,
    BodyViolationKind,
    render_violations,
    validate_body,
)

MEMBER_CODE = "01613058"

#: 決まりを全部満たした本文 (600〜700字程度)。
GOOD = (
    f"{HEADLINE}\n"
    f"{MEMBER_CODE}様（システム上お名前が表示されず、会員番号でのご挨拶となる失礼を"
    "お許しください）\n"
    "はじめまして。ヤガサキ歯科医院で院長をしております矢ケ崎と申します。\n"
    "プロフィールを拝見して、歯科衛生士として三年、患者様と向き合ってこられた"
    "積み重ねに目が留まりました。三年というのは、器具の扱いに迷いがなくなり、"
    "同時に患者様の小さな変化に気づけるようになる時期だと感じています。日々の"
    "処置の中で、次はこの方にどう伝えようかと考える場面が増えてくる頃ではないで"
    "しょうか。\n"
    "川崎市多摩区にお住まいとのことですので、同じ市内ですから通勤はしやすいかと"
    "思います。お車でしたら十五分ほどかと思いますが、実際には道の混み具合で前後"
    "するかもしれませんので、もし気になるようでしたら通勤手段も含めて一緒に確認"
    "させていただきます。\n"
    "患者様との関係を長く育てていきたいとお考えの方には、一人の患者様を最後まで"
    "担当し、四十五分かけて向き合える当院の体制が、そのまま力になるはずです。"
    "また、これから知識を広げたいというお気持ちがあるなら、専門医と認定医が"
    "在籍しておりますので、迷ったその場で相談できる距離の近さが後押しになると"
    "思います。\n"
    "働き方はライフイベントで変わるものだと考えておりますので、そのときどきに"
    "合わせてスタッフ全員で支え合っています。\n"
    "まずはお話だけでも、見学だけでも大歓迎です！お会いできたら嬉しいです。\n"
    f"ご興味をお持ちいただけましたら、{APPLY_BUTTON}ボタンを押してください。\n"
    "ヤガサキ歯科医院 院長 矢ケ崎 隆信"
)


def _kinds(body: str, code: str = MEMBER_CODE) -> set[BodyViolationKind]:
    return {violation.kind for violation in validate_body(body, member_code=code)}


def test_a_body_that_follows_the_prompt_passes() -> None:
    assert validate_body(GOOD, member_code=MEMBER_CODE) == ()
    assert "規則違反はありません" in render_violations(())


# ---------------------------------------------------------------------------
# 必ず入っていなければならないもの
# ---------------------------------------------------------------------------


def test_the_headline_is_required() -> None:
    assert BodyViolationKind.MISSING_HEADLINE in _kinds(GOOD.replace(HEADLINE, ""))


def test_the_apply_button_is_required() -> None:
    assert BodyViolationKind.MISSING_APPLY_BUTTON in _kinds(GOOD.replace(APPLY_BUTTON, ""))


def test_the_wrong_brackets_are_a_different_violation_from_a_missing_one() -> None:
    """**「無い」と「書き方が違う」は直し方が違う。** まとめて1つにしない。

    カギ括弧で書かれると、媒体の画面上のボタン名と一致しなくなる。
    """
    wrong = GOOD.replace(APPLY_BUTTON, "「このスカウトに応募する」")
    kinds = _kinds(wrong)
    assert BodyViolationKind.APPLY_BUTTON_WRONG_BRACKETS in kinds
    assert BodyViolationKind.MISSING_APPLY_BUTTON not in kinds


def test_the_member_code_salutation_is_required() -> None:
    """**氏名が取れない媒体で、宛名を「様」だけにしない。**"""
    assert BodyViolationKind.MISSING_SALUTATION in _kinds(GOOD.replace(MEMBER_CODE, ""))


def test_an_unfilled_marker_is_caught_before_it_reaches_the_candidate() -> None:
    """**このまま送ると、記法がそのまま候補者へ届く。**"""
    assert BodyViolationKind.UNFILLED_SLOT in _kinds(GOOD + "\n{{SELF_PR}}")


# ---------------------------------------------------------------------------
# 絶対禁止事項
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("term", ["パート", "アルバイト", "時給"])
def test_employment_terms_for_a_full_time_only_posting_are_refused(term: str) -> None:
    """正社員のみの募集なので、これらの語が出た時点で誤解を招く。"""
    assert BodyViolationKind.FORBIDDEN_TERM in _kinds(GOOD + f"\n{term}のご相談も可能です。")


@pytest.mark.parametrize("marker", ["**", "__", "<b>", "<strong>"])
def test_bold_markup_is_refused(marker: str) -> None:
    assert BodyViolationKind.BOLD in _kinds(GOOD + f"\n{marker}強調{marker}")


@pytest.mark.parametrize("char", ["*", "＊"])
def test_asterisks_are_refused(char: str) -> None:
    assert BodyViolationKind.ASTERISK in _kinds(GOOD + f"\n{char} 補足")


@pytest.mark.parametrize("prefix", ["・", "-", "●", "☑", "✅", "1. ", "１．"])
def test_bullet_lists_are_refused(prefix: str) -> None:
    assert BodyViolationKind.BULLET_LIST in _kinds(GOOD + f"\n{prefix}当院の魅力")


def test_a_nakaguro_inside_a_sentence_is_not_a_bullet() -> None:
    """**行頭に限る。** 「・」は文中の並列にも使われる。

    どこでも禁止にすると正常な文が落ちる。プロンプトが禁じているのは箇条書きと
    いう **書式** である。
    """
    inline = GOOD.replace(
        "働き方はライフイベントで変わるものだと考えておりますので、",
        "予防・歯周治療を大切にしており、",
    )
    assert BodyViolationKind.BULLET_LIST not in _kinds(inline)


def test_the_headline_itself_is_not_read_as_a_bullet() -> None:
    """見出しの表示は箇条書きではない。除外しないと1行目で必ず落ちる。"""
    assert BodyViolationKind.BULLET_LIST not in _kinds(GOOD)


# ---------------------------------------------------------------------------
# 長さ
# ---------------------------------------------------------------------------


def test_a_short_body_is_refused() -> None:
    body = f"{HEADLINE}\n{MEMBER_CODE}様\n{APPLY_BUTTON}"
    assert BodyViolationKind.TOO_SHORT in _kinds(body)


def test_a_long_body_is_refused() -> None:
    assert BodyViolationKind.TOO_LONG in _kinds(GOOD + "あ" * 900)


def test_whitespace_does_not_count_toward_the_length() -> None:
    """**改行を数えに入れない。** 段落を増やしただけで判定が動くのは違う。"""
    spaced = GOOD.replace("\n", "\n\n\n")
    kinds = _kinds(spaced)
    assert BodyViolationKind.TOO_LONG not in kinds
    assert BodyViolationKind.TOO_SHORT not in kinds


# ---------------------------------------------------------------------------
# 報告
# ---------------------------------------------------------------------------


def test_the_report_names_every_violation_without_quoting_the_body() -> None:
    """**本文は載せない** (13.2)。載せると個人向けの文面がログに残る。"""
    body = GOOD.replace(HEADLINE, "").replace(APPLY_BUTTON, "") + "\n時給のご相談も可能です。"
    violations = validate_body(body, member_code=MEMBER_CODE)
    report = render_violations(violations)
    assert "3 件の規則違反" in report
    # 本文の固有の文言が報告に出ていないこと。
    assert "矢ケ崎" not in report
    assert MEMBER_CODE not in report
