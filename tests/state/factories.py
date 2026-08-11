"""Builders for the pure state tests.

DBには一切触れない。ここで組み立てるのは :class:`SendRecord` の値だけで、
判定 (9.1/9.2) が純粋関数であることをそのまま利用している。
"""

from __future__ import annotations

from datetime import UTC, datetime

from jobmedley_scout.models.send_record import MessageKind, SendRecord, SendSlot, SendStatus

FIXED_INSTANT = datetime(2026, 4, 1, 3, 0, tzinfo=UTC)


def make_send_record(
    *,
    status: SendStatus,
    record_id: int = 1,
    candidate_id: str = "C-0001",
    idempotency_key: str = "key-existing",
    message_kind: MessageKind = MessageKind.FIRST_CONTACT,
    followup_seq: int = 0,
    slot: SendSlot = SendSlot.FREE,
    endpoint_id: str = "endpoint-a",
    subject: str = "はじめまして",
    sent_at: datetime | None = None,
    failure_reason: str | None = None,
) -> SendRecord:
    return SendRecord(
        record_id=record_id,
        candidate_id=candidate_id,
        idempotency_key=idempotency_key,
        message_kind=message_kind,
        followup_seq=followup_seq,
        slot=slot,
        endpoint_id=endpoint_id,
        subject=subject,
        status=status,
        reserved_at=FIXED_INSTANT,
        sent_at=sent_at,
        failure_reason=failure_reason,
    )
