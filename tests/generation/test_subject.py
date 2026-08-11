"""10.2: 件名は返信検知の突合キーである。短い件名も重複した件名も送らない。"""

from __future__ import annotations

import pytest

from jobmedley_scout.errors import GenerationError
from jobmedley_scout.generation.subject import (
    MIN_SUBJECT_NORM_LENGTH,
    build_subject,
    subject_keys,
    validate_subject_format,
)
from tests.generation.helpers import PREFIX_LEN, make_candidate, make_clock, make_core


def test_subject_has_the_expected_shape() -> None:
    subject = build_subject(
        make_candidate(display_name="田中太郎"),
        make_core(subject="介護のお仕事のご紹介"),
        make_clock(),
        already_used_subjects=(),
        prefix_len=PREFIX_LEN,
    )

    assert subject == "田中太郎様｜介護のお仕事のご紹介｜8/11"
    assert validate_subject_format(subject) is True


def test_keys_are_derived_with_the_shared_normalizer() -> None:
    keys = subject_keys("田中太郎様｜介護のお仕事のご紹介｜8/11", PREFIX_LEN)

    assert " " not in keys.subject_norm
    assert keys.subject_norm.startswith("田中太郎様|")
    assert keys.subject_prefix35 == keys.subject_norm[:PREFIX_LEN]

    # 受信箱の "Re: " は正規化で剥がれる。剥がれないと完全一致が当たらない。
    assert subject_keys("Re: " + keys.subject, PREFIX_LEN).subject_norm == keys.subject_norm


def test_subject_shorter_than_the_minimum_is_refused() -> None:
    """短い件名は他人の返信と誤って突合する。送らずに落とす。"""
    with pytest.raises(GenerationError) as excinfo:
        build_subject(
            make_candidate(display_name="あ"),
            make_core(subject="案"),
            make_clock(),
            already_used_subjects=(),
            prefix_len=PREFIX_LEN,
        )

    assert str(MIN_SUBJECT_NORM_LENGTH) in str(excinfo.value)


def test_duplicate_subject_in_the_same_run_is_refused() -> None:
    candidate = make_candidate(display_name="田中太郎")
    core = make_core(subject="介護のお仕事のご紹介")
    first = build_subject(
        candidate, core, make_clock(), already_used_subjects=(), prefix_len=PREFIX_LEN
    )

    with pytest.raises(GenerationError, match="重複"):
        build_subject(
            make_candidate(candidate_id="10002", display_name="田中太郎"),
            core,
            make_clock(),
            already_used_subjects=(first,),
            prefix_len=PREFIX_LEN,
        )


def test_colliding_prefix_key_is_refused() -> None:
    """前方一致キーが衝突すると、その返信は複数該当 (AMBIGUOUS) として捨てられる。"""
    clock = make_clock()
    first = build_subject(
        make_candidate(display_name="田中太郎"),
        make_core(subject="介護のお仕事のご紹介です"),
        clock,
        already_used_subjects=(),
        prefix_len=6,
    )

    with pytest.raises(GenerationError, match="前方一致"):
        build_subject(
            make_candidate(candidate_id="10002", display_name="田中太郎"),
            make_core(subject="別のご案内でございます"),
            clock,
            already_used_subjects=(first,),
            prefix_len=6,
        )


def test_different_candidates_do_not_collide() -> None:
    clock = make_clock()
    first = build_subject(
        make_candidate(candidate_id="10001", display_name="田中太郎"),
        make_core(),
        clock,
        already_used_subjects=(),
        prefix_len=PREFIX_LEN,
    )
    second = build_subject(
        make_candidate(candidate_id="10002", display_name="鈴木一郎"),
        make_core(),
        clock,
        already_used_subjects=(first,),
        prefix_len=PREFIX_LEN,
    )

    assert first != second


def test_honorific_already_on_the_name_is_not_doubled() -> None:
    subject = build_subject(
        make_candidate(display_name="田中太郎様"),
        make_core(),
        make_clock(),
        already_used_subjects=(),
        prefix_len=PREFIX_LEN,
    )

    assert subject.startswith("田中太郎様｜")


def test_separator_in_the_generated_subject_is_stripped() -> None:
    """区切り文字が素材に混ざると形式検査もログの読み取りも壊れる。"""
    subject = build_subject(
        make_candidate(display_name="田中太郎"),
        make_core(subject="介護｜お仕事のご紹介"),
        make_clock(),
        already_used_subjects=(),
        prefix_len=PREFIX_LEN,
    )

    assert subject.count("｜") == 2
    assert validate_subject_format(subject) is True


def test_long_generated_subject_is_truncated() -> None:
    subject = build_subject(
        make_candidate(display_name="田中太郎"),
        make_core(subject="あ" * 200),
        make_clock(),
        already_used_subjects=(),
        prefix_len=PREFIX_LEN,
    )

    assert subject.count("あ") == 60


def test_empty_generated_subject_is_refused() -> None:
    with pytest.raises(GenerationError):
        build_subject(
            make_candidate(),
            make_core(subject="   "),
            make_clock(),
            already_used_subjects=(),
            prefix_len=PREFIX_LEN,
        )


def test_non_positive_prefix_length_is_refused() -> None:
    with pytest.raises(GenerationError):
        subject_keys("田中太郎様｜介護のお仕事のご紹介｜8/11", 0)


def test_format_check_rejects_a_hand_written_subject() -> None:
    assert validate_subject_format("ご紹介") is False
    assert validate_subject_format("田中太郎様｜介護のご紹介") is False
