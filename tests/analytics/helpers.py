"""Fixtures for the analytics tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from jobmedley_scout.clock import JST, FixedClock
from jobmedley_scout.config.schema import AnalyticsConfig, AnalyticsSinkKind
from jobmedley_scout.models.provenance import AUTO_SUBJECT_MATCH
from jobmedley_scout.models.reply import MatchKind, ReplyDetection
from jobmedley_scout.models.send_record import (
    MessageKind,
    SendRecord,
    SendSlot,
    SendStatus,
)


def jst(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    """A JST wall-clock instant, as the aware UTC datetime the system stores."""
    return datetime(year, month, day, hour, minute, tzinfo=JST).astimezone(UTC)


def make_clock(instant: datetime | None = None) -> FixedClock:
    return FixedClock(instant if instant is not None else jst(2026, 8, 11))


def make_config(
    *,
    weekly_periods: int = 26,
    monthly_periods: int = 12,
    trend_weeks: int = 8,
    trend_months: int = 6,
    trend_min_sample: int = 10,
    output_dir: Path | None = None,
) -> AnalyticsConfig:
    return AnalyticsConfig(
        sink=AnalyticsSinkKind.LOCAL,
        weekly_periods=weekly_periods,
        monthly_periods=monthly_periods,
        trend_weeks=trend_weeks,
        trend_months=trend_months,
        trend_min_sample=trend_min_sample,
        output_dir=output_dir if output_dir is not None else Path("artifacts/analytics"),
        spreadsheet_id=None,
    )


_next_record_id = [0]


def make_send(
    candidate_id: str,
    sent_at: datetime | None,
    *,
    slot: SendSlot = SendSlot.FREE,
    status: SendStatus = SendStatus.SENT,
    kind: MessageKind = MessageKind.FIRST_CONTACT,
    record_id: int | None = None,
) -> SendRecord:
    _next_record_id[0] += 1
    resolved_id = record_id if record_id is not None else _next_record_id[0]
    return SendRecord(
        record_id=resolved_id,
        candidate_id=candidate_id,
        idempotency_key=f"key-{resolved_id}",
        message_kind=kind,
        followup_seq=0 if kind is MessageKind.FIRST_CONTACT else 1,
        slot=slot,
        endpoint_id="scout.send",
        subject=f"{candidate_id}様｜ご案内｜1/1",
        status=status,
        reserved_at=sent_at if sent_at is not None else jst(2026, 1, 1),
        sent_at=sent_at,
        failure_reason=None,
    )


def make_reply(
    candidate_id: str,
    replied_at: datetime | None = None,
    *,
    provenance: str = AUTO_SUBJECT_MATCH,
    run_id: str = "run-1",
) -> ReplyDetection:
    return ReplyDetection(
        run_id=run_id,
        candidate_id=candidate_id,
        send_record_id=None,
        matched_subject_norm=f"{candidate_id}様|ご案内|1/1",
        match_kind=MatchKind.EXACT,
        provenance=provenance,
        replied_at=replied_at,
    )
