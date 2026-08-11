"""9.1 -- only ``SENT`` counts as sent."""

from __future__ import annotations

import pytest

from jobmedley_scout.models.send_record import MessageKind, SendStatus
from jobmedley_scout.state.dedupe import (
    RETRYABLE_STATUSES,
    SENT_STATUSES,
    is_already_sent,
    retryable_records,
    sent_records,
)
from tests.state.factories import make_send_record


def test_sent_is_already_sent() -> None:
    records = (make_send_record(status=SendStatus.SENT),)
    assert is_already_sent(records, MessageKind.FIRST_CONTACT) is True


@pytest.mark.parametrize(
    "status",
    [SendStatus.GENERATED, SendStatus.SENDING, SendStatus.FAILED, SendStatus.SKIPPED],
    ids=lambda s: str(s),
)
def test_every_other_status_remains_retryable(status: SendStatus) -> None:
    """``SENDING`` を「送信済み」に丸めない。丸めると送信直後に落ちた対象が
    二度と送られず、しかもエラーは一切出ない (9.1/9.2)。"""
    records = (make_send_record(status=status),)
    assert is_already_sent(records, MessageKind.FIRST_CONTACT) is False
    assert retryable_records(records, MessageKind.FIRST_CONTACT) == records


def test_no_records_is_not_already_sent() -> None:
    assert is_already_sent((), MessageKind.FIRST_CONTACT) is False


def test_the_sent_set_contains_only_sent() -> None:
    assert frozenset({SendStatus.SENT}) == SENT_STATUSES
    assert SendStatus.SENDING in RETRYABLE_STATUSES
    assert SENT_STATUSES.isdisjoint(RETRYABLE_STATUSES)
    # 状態はどちらかに必ず属する。増えたときに分類漏れで落ちるようにしておく。
    assert set(SendStatus) == SENT_STATUSES | RETRYABLE_STATUSES


def test_a_failed_attempt_followed_by_a_sent_one_counts_as_sent() -> None:
    records = (
        make_send_record(record_id=1, status=SendStatus.FAILED),
        make_send_record(record_id=2, status=SendStatus.SENT),
    )
    assert is_already_sent(records, MessageKind.FIRST_CONTACT) is True
    # 既送信があるなら再試行対象は無い -- あれば二重送信になる。
    assert retryable_records(records, MessageKind.FIRST_CONTACT) == ()


def test_a_send_to_another_kind_does_not_block_this_one() -> None:
    records = (make_send_record(status=SendStatus.SENT, message_kind=MessageKind.FIRST_CONTACT),)
    assert is_already_sent(records, MessageKind.FOLLOW_UP, followup_seq=1) is False


def test_followup_sequence_numbers_are_independent() -> None:
    """通番を無視すると、1通目を送った相手に2通目が永久に送られない。"""
    records = (
        make_send_record(
            status=SendStatus.SENT, message_kind=MessageKind.FOLLOW_UP, followup_seq=1
        ),
    )
    assert is_already_sent(records, MessageKind.FOLLOW_UP, followup_seq=1) is True
    assert is_already_sent(records, MessageKind.FOLLOW_UP, followup_seq=2) is False


def test_sent_records_expose_the_evidence() -> None:
    records = (
        make_send_record(record_id=1, status=SendStatus.SENDING),
        make_send_record(record_id=2, status=SendStatus.SENT),
        make_send_record(record_id=3, status=SendStatus.SENT),
    )
    duplicated = sent_records(records, MessageKind.FIRST_CONTACT)
    # 2件以上あるということは既に二重送信が起きている。件数で示せること自体が要件 (12.8)。
    assert [record.record_id for record in duplicated] == [2, 3]


def test_retryable_records_keep_the_sending_row_for_key_reuse() -> None:
    """再試行対象に ``SENDING`` が残ることが 9.2 のキー再利用の前提。"""
    records = (
        make_send_record(record_id=1, status=SendStatus.FAILED),
        make_send_record(record_id=2, status=SendStatus.SENDING),
    )
    retryable = retryable_records(records, MessageKind.FIRST_CONTACT)
    assert [record.status for record in retryable] == [SendStatus.FAILED, SendStatus.SENDING]
