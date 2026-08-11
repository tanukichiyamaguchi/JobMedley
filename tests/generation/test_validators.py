"""8.5 + 8.7: 検査は **最終本文** に掛ける。そして自分の装飾で自分を踏まない。"""

from __future__ import annotations

from jobmedley_scout.generation.assemble import (
    INTRO_BULLET,
    SECTION_RULE_CHAR,
    SYSTEM_GLYPHS,
    URL_ARROW,
    assemble,
)
from jobmedley_scout.generation.validators import (
    MAX_CORRECTION_RETRIES,
    ViolationKind,
    _is_symbol,
    validate,
)
from jobmedley_scout.models.message import AssembledMessage, Introduction
from tests.generation.helpers import (
    make_context,
    make_core,
    make_generation_config,
)


def _kinds(message: AssembledMessage, *, had_commonality: bool = True) -> set[ViolationKind]:
    return {item.kind for item in validate(message, make_generation_config(), had_commonality)}


def test_a_normal_message_passes_cleanly() -> None:
    message = assemble(make_core(), make_context())

    assert validate(message, make_generation_config(), True) == ()


def test_the_systems_own_footer_does_not_trigger_the_emoji_check() -> None:
    """参照実装は自前のフッターの ◎ で自分のバリデータを踏み、全メッセージで
    修正リトライを走らせていた (8.5)。ここが赤くなったら、それが再発している。"""
    message = assemble(make_core(), make_context())

    assert ViolationKind.EMOJI not in _kinds(message)


def test_the_glyph_exclusion_is_load_bearing() -> None:
    """除外していなければ引っかかる記号であることを明示する。
    「なぜこの除外があるのか」を消さないための記録でもある。"""
    for glyph in (INTRO_BULLET, SECTION_RULE_CHAR, URL_ARROW):
        assert _is_symbol(glyph) is True
        assert glyph in SYSTEM_GLYPHS


def test_a_real_emoji_is_caught() -> None:
    message = assemble(make_core(closing="ご返信お待ちしております😊"), make_context())

    assert ViolationKind.EMOJI in _kinds(message)


def test_japanese_business_symbols_are_not_treated_as_emoji() -> None:
    """〒 や ※ や ① を弾くと、運用者が検査ごと切りたくなる。"""
    message = assemble(
        make_core(closing="※ご返信は①のリンクからお願いします。"),
        make_context(footer_lines=("〒100-0001 東京都千代田区1-1-1",)),
    )

    assert ViolationKind.EMOJI not in _kinds(message)


def test_a_url_outside_the_allowlist_is_caught() -> None:
    message = assemble(
        make_core(motivation="詳しくは https://example.com/jobs をご覧ください。"),
        make_context(),
    )

    violations = validate(message, make_generation_config(), True)
    urls = [item for item in violations if item.kind is ViolationKind.URL_NOT_ALLOWED]
    assert len(urls) == 1
    assert "example.com" in urls[0].evidence


def test_allowlisted_subdomains_are_accepted() -> None:
    message = assemble(
        make_core(), make_context(footer_url="https://customers.job-medley.com/scout/")
    )

    assert ViolationKind.URL_NOT_ALLOWED not in _kinds(message)


def test_a_lookalike_domain_is_not_accepted() -> None:
    """job-medley.com.evil.example を許可リスト一致にしない。"""
    message = assemble(make_core(), make_context(footer_url="https://job-medley.com.evil.example/"))

    assert ViolationKind.URL_NOT_ALLOWED in _kinds(message)


def test_an_email_address_in_the_signature_is_not_read_as_a_url() -> None:
    """署名のメールアドレスを URL と誤検知すると全件違反になる (8.5 と同じ自爆)。"""
    message = assemble(
        make_core(), make_context(signature_lines=("佐藤 花子", "scout@example.co.jp"))
    )

    assert ViolationKind.URL_NOT_ALLOWED not in _kinds(message)


def test_assertive_terms_are_flagged_only_without_a_commonality() -> None:
    core = make_core(
        introductions=(
            Introduction(person_id="p-001", blurb="鈴木も同じ介護の現場から転職しています。"),
        )
    )
    message = assemble(core, make_context())

    assert ViolationKind.ASSERTIVE_WITHOUT_COMMONALITY in _kinds(message, had_commonality=False)
    assert ViolationKind.ASSERTIVE_WITHOUT_COMMONALITY not in _kinds(message, had_commonality=True)


def test_soft_terms_are_allowed_without_a_commonality() -> None:
    """「近い」「近しい」は断定しないラベルなので、共通点が無くても使ってよい。"""
    core = make_core(
        introductions=(Introduction(person_id="p-001", blurb="鈴木は近い経歴を持つメンバーです。"),)
    )
    message = assemble(core, make_context())

    assert ViolationKind.ASSERTIVE_WITHOUT_COMMONALITY not in _kinds(message, had_commonality=False)


def test_assertive_terms_are_checked_on_the_assembled_text_not_just_the_core() -> None:
    """8.5: 検査対象は最終本文。コードが足す定型文も検査を通る。"""
    message = assemble(
        make_core(),
        make_context(boilerplate=("弊社も同様に介護業界の出身です。",)),
    )

    assert ViolationKind.ASSERTIVE_WITHOUT_COMMONALITY in _kinds(message, had_commonality=False)


def test_too_many_exclamation_marks_are_caught() -> None:
    message = assemble(
        make_core(closing="ぜひお話ししましょう！！ご連絡お待ちしています！"),
        make_context(),
    )

    assert ViolationKind.TOO_MANY_EXCLAMATIONS in _kinds(message)


def test_a_broken_subject_key_is_caught() -> None:
    """突合キーがずれたまま送ると、その対象の返信は恒久的に検知できない。"""
    good = assemble(make_core(), make_context())
    broken = AssembledMessage(
        subject=good.subject,
        body=good.body,
        subject_norm="まったく別の文字列",
        subject_prefix35=good.subject_prefix35,
    )

    assert ViolationKind.SUBJECT_KEY_MISMATCH in _kinds(broken)


def test_a_hand_written_subject_is_flagged_for_format() -> None:
    good = assemble(make_core(), make_context())
    odd = AssembledMessage(
        subject="ご紹介",
        body=good.body,
        subject_norm="ご紹介",
        subject_prefix35="ご紹介",
    )

    kinds = _kinds(odd)
    assert ViolationKind.SUBJECT_FORMAT in kinds
    assert ViolationKind.SUBJECT_TOO_SHORT in kinds


def test_correction_retry_budget_is_a_single_shared_constant() -> None:
    """呼び出し側ごとに「もう1回だけ」を足すと事実上の無限ループになる。"""
    assert MAX_CORRECTION_RETRIES == 1
