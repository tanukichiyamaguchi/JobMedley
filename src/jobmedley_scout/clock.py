"""Time access, centralized.

This module is the **only** place in the codebase allowed to read the wall
clock. Everything else takes a ``Clock`` and is therefore testable without
freezing global state. ``tests/guardrails/test_no_direct_clock.py`` enforces it
with a source scan.

Rationale: the business calendar (12.4), cohort attribution (11.3) and the
idempotency/rotation logic (9.2, 9.6) all make decisions from "now". If "now"
is not injectable, none of those decisions can be unit-tested, and this system's
whole testability argument (13.4) rests on them being pure.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

# 媒体・業務ともに日本時間で運用される。営業日判定 (12.4) と週次コホート
# (11.3) は JST で切ること。UTC で切ると週境界が9時間ずれる。
JST = ZoneInfo("Asia/Tokyo")


class Clock(Protocol):
    """Injectable source of the current time."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The real clock. Used in production; never in unit tests."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FixedClock:
    """A clock frozen at a given instant, for tests."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        return self._instant

    def advance(self, delta: timedelta) -> None:
        self._instant += delta


def to_jst(instant: datetime) -> datetime:
    """Convert an instant to JST. Raises on naive datetimes."""
    if instant.tzinfo is None:
        raise ValueError("naive datetime; every instant in this system is tz-aware")
    return instant.astimezone(JST)


def jst_date(instant: datetime) -> date:
    """The calendar date in JST -- the date the business operates on."""
    return to_jst(instant).date()


def isoformat_utc(instant: datetime) -> str:
    """Canonical string form for persistence.

    All timestamps are stored as ISO-8601 UTC so that string ordering equals
    chronological ordering in SQLite (which has no native datetime type).
    """
    if instant.tzinfo is None:
        raise ValueError("naive datetime; every instant in this system is tz-aware")
    return instant.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    """Inverse of :func:`isoformat_utc`."""
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
