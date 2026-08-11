"""10.2: 件名が結合キーである。短い件名・曖昧な件名は照合しない。"""

from __future__ import annotations

import pytest

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.generation.subject import (
    DEFAULT_SUBJECT_PREFIX_LENGTH,
    MIN_SUBJECT_NORM_LENGTH,
)
from jobmedley_scout.models.reply import MatchKind, MatchOutcome
from jobmedley_scout.models.text_norm import normalize_subject
from jobmedley_scout.reply.subject_match import (
    DEFAULT_PREFIX_LENGTH,
    MIN_SUBJECT_MATCH_LENGTH,
    SubjectEntry,
    SubjectIndex,
    match_subject,
)

PREFIX_LEN = DEFAULT_PREFIX_LENGTH

#: 正規化後が前方一致長より十分長い件名。前方一致の検証に使う。
LONG_SUBJECT = "田中太郎様｜訪問介護のお仕事をご紹介させていただきたくご連絡しました｜8/11"


def make_entry(
    subject: str,
    *,
    candidate_id: str = "10001",
    send_record_id: int | None = 1,
    prefix_length: int = PREFIX_LEN,
) -> SubjectEntry:
    normalized = normalize_subject(subject)
    return SubjectEntry(
        candidate_id=candidate_id,
        send_record_id=send_record_id,
        subject_norm=normalized,
        subject_prefix35=normalized[:prefix_length],
    )


def test_the_long_fixture_is_long_enough_to_exercise_prefix_matching() -> None:
    """前提が崩れたら前方一致のテストが完全一致になってしまうので固定する。"""
    assert len(normalize_subject(LONG_SUBJECT)) > PREFIX_LEN


def test_exact_subject_matches_the_candidate() -> None:
    index = SubjectIndex([make_entry(LONG_SUBJECT, candidate_id="10001", send_record_id=7)])

    match = match_subject(LONG_SUBJECT, index)

    assert match.outcome is MatchOutcome.MATCHED
    assert match.candidate_id == "10001"
    assert match.send_record_id == 7
    assert match.match_kind is MatchKind.EXACT


def test_reply_prefixed_subject_matches_the_same_key() -> None:
    """受信箱の行は「Re: <送信した件名>」で現れる。転送を経ると Re: が積み重なる。"""
    index = SubjectIndex([make_entry(LONG_SUBJECT)])

    single = match_subject(f"Re: {LONG_SUBJECT}", index)
    stacked = match_subject(f"Re: Re: {LONG_SUBJECT}", index)
    japanese = match_subject(f"返信： {LONG_SUBJECT}", index)

    for match in (single, stacked, japanese):
        assert match.outcome is MatchOutcome.MATCHED
        assert match.candidate_id == "10001"
        assert match.match_kind is MatchKind.EXACT


def test_subject_truncated_by_the_platform_matches_on_the_prefix() -> None:
    """媒体が件名を途中で切ると完全一致は当たらない。前方一致だけが頼りになる。"""
    normalized = normalize_subject(LONG_SUBJECT)
    truncated = normalized[: PREFIX_LEN + 3]
    index = SubjectIndex([make_entry(LONG_SUBJECT, candidate_id="10001", send_record_id=7)])

    match = match_subject(f"Re: {truncated}", index)

    assert match.outcome is MatchOutcome.MATCHED
    assert match.candidate_id == "10001"
    assert match.match_kind is MatchKind.PREFIX35


def test_subject_with_trailing_noise_matches_on_the_prefix() -> None:
    index = SubjectIndex([make_entry(LONG_SUBJECT)])

    match = match_subject(f"Re: {LONG_SUBJECT} (転送)", index)

    assert match.outcome is MatchOutcome.MATCHED
    assert match.match_kind is MatchKind.PREFIX35


def test_subject_shorter_than_the_minimum_is_refused() -> None:
    """11文字は突合キーとして短すぎる。他人の返信に当たるので照合しない。"""
    short = "あ" * 11
    assert len(normalize_subject(short)) == MIN_SUBJECT_MATCH_LENGTH - 1
    index = SubjectIndex([make_entry(short)])

    match = match_subject(short, index)

    assert match.outcome is MatchOutcome.TOO_SHORT
    assert match.candidate_id is None
    assert match.send_record_id is None
    assert match.match_kind is None


def test_a_subject_at_exactly_the_minimum_is_still_matched() -> None:
    """境界の内側は通す。下限を上げると生成側が通した件名が静かに捨てられる。"""
    exact_minimum = "あ" * MIN_SUBJECT_MATCH_LENGTH
    index = SubjectIndex([make_entry(exact_minimum)])

    assert match_subject(exact_minimum, index).outcome is MatchOutcome.MATCHED


def test_one_subject_shared_by_two_candidates_is_ambiguous_and_does_not_match() -> None:
    """曖昧は「照合しない」。誤検知は一度DBに入ると手作業では消せない (10.4)。"""
    index = SubjectIndex(
        [
            make_entry(LONG_SUBJECT, candidate_id="10001", send_record_id=1),
            make_entry(LONG_SUBJECT, candidate_id="10002", send_record_id=2),
        ]
    )

    match = match_subject(f"Re: {LONG_SUBJECT}", index)

    assert match.outcome is MatchOutcome.AMBIGUOUS
    assert match.candidate_id is None
    assert match.send_record_id is None
    assert match.match_kind is None
    assert match.ambiguous_candidate_ids == ("10001", "10002")


def test_ambiguity_on_the_prefix_key_also_refuses_to_match() -> None:
    """完全一致が割れていなくても、前方一致で割れたら同じく照合しない。"""
    normalized = normalize_subject(LONG_SUBJECT)
    other = f"{LONG_SUBJECT}の追記"
    assert normalize_subject(other)[:PREFIX_LEN] == normalized[:PREFIX_LEN]
    index = SubjectIndex(
        [
            make_entry(LONG_SUBJECT, candidate_id="10001"),
            make_entry(other, candidate_id="10002"),
        ]
    )

    match = match_subject(normalized[: PREFIX_LEN + 2], index)

    assert match.outcome is MatchOutcome.AMBIGUOUS
    assert match.ambiguous_candidate_ids == ("10001", "10002")


def test_two_sends_to_the_same_candidate_match_without_a_send_record() -> None:
    """候補者は一意なので照合する。どちらの送信への返信かは決められないので空にする。"""
    index = SubjectIndex(
        [
            make_entry(LONG_SUBJECT, candidate_id="10001", send_record_id=1),
            make_entry(LONG_SUBJECT, candidate_id="10001", send_record_id=2),
        ]
    )

    match = match_subject(LONG_SUBJECT, index)

    assert match.outcome is MatchOutcome.MATCHED
    assert match.candidate_id == "10001"
    assert match.send_record_id is None


def test_unknown_subject_is_no_match_not_ambiguous() -> None:
    index = SubjectIndex([make_entry(LONG_SUBJECT)])

    match = match_subject("山田花子様｜まったく別の件名でのご連絡です｜9/1", index)

    assert match.outcome is MatchOutcome.NO_MATCH
    assert match.ambiguous_candidate_ids == ()


def test_entries_without_a_subject_are_not_indexed() -> None:
    """件名を失った送信記録は照合できない (13.3)。索引には入れず、件数だけ残す。"""
    index = SubjectIndex(
        [
            SubjectEntry(
                candidate_id="10001", send_record_id=1, subject_norm="", subject_prefix35=""
            ),
            make_entry(LONG_SUBJECT, candidate_id="10002"),
        ]
    )

    assert len(index) == 1
    assert len(index.ignored_entries) == 1
    assert match_subject(LONG_SUBJECT, index).candidate_id == "10002"


def test_stored_subject_is_renormalized_on_the_way_in() -> None:
    """DBの値が古い正規化で書かれていても照合側と同じ形に揃う (8.6)。"""
    entry = SubjectEntry(
        candidate_id="10001",
        send_record_id=1,
        subject_norm=f"Re: {LONG_SUBJECT}",
        subject_prefix35="",
    )

    assert entry.subject_norm == normalize_subject(LONG_SUBJECT)


def test_prefix_key_is_derived_not_trusted() -> None:
    """保存列が古い長さのままでも照合は当たる。ずれた件数は診断として残す。"""
    normalized = normalize_subject(LONG_SUBJECT)
    stale = SubjectEntry(
        candidate_id="10001",
        send_record_id=1,
        subject_norm=normalized,
        # 20文字で切られた時代の保存列。
        subject_prefix35=normalized[:20],
    )
    index = SubjectIndex([stale])

    assert index.prefix_mismatches == (stale,)
    assert match_subject(normalized[: PREFIX_LEN + 3], index).match_kind is MatchKind.PREFIX35


def test_prefix_length_disagreement_is_loud() -> None:
    """静かに当たらなくなるくらいなら実行を落とす (8.6)。"""
    index = SubjectIndex([make_entry(LONG_SUBJECT)], prefix_length=35)

    with pytest.raises(ConfigError, match="前方一致長"):
        match_subject(LONG_SUBJECT, index, prefix_length=40)


def test_non_positive_prefix_length_is_refused() -> None:
    with pytest.raises(ConfigError):
        SubjectIndex([make_entry(LONG_SUBJECT)], prefix_length=0)


def test_matching_constants_are_shared_with_generation() -> None:
    """生成側と照合側で定数がずれると返信が静かに失われる (8.6)。"""
    assert MIN_SUBJECT_MATCH_LENGTH == MIN_SUBJECT_NORM_LENGTH
    assert DEFAULT_PREFIX_LENGTH == DEFAULT_SUBJECT_PREFIX_LENGTH
