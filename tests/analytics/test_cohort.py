"""11.3: 返信は初回送信の週・月に帰属する。受信日ではない。"""

from __future__ import annotations

import pytest

from jobmedley_scout.analytics.cohort import (
    Cohort,
    Granularity,
    cohort_of,
    is_recent_cohort,
)
from tests.analytics.helpers import jst


def test_cohort_is_the_week_and_month_of_the_send() -> None:
    cohort = cohort_of(jst(2026, 8, 11))

    assert cohort == Cohort(iso_week="2026-W33", month="2026-08")


def test_cohort_boundary_is_cut_in_jst_not_utc() -> None:
    """UTC で切ると月曜早朝の送信が前週に落ちる (9時間ずれる)。"""
    # 2026-08-10 (月) 00:30 JST は UTC ではまだ 08-09 (日) 15:30。
    monday_early = jst(2026, 8, 10, hour=0, minute=30)

    assert cohort_of(monday_early).iso_week == "2026-W33"
    # 直前の日曜は前週。
    assert cohort_of(jst(2026, 8, 9, hour=23)).iso_week == "2026-W32"


def test_new_year_week_uses_the_iso_week_year() -> None:
    """12/31 が翌年の W01 になる年がある。local.year を使うと2週間だけ迷子になる。"""
    assert cohort_of(jst(2025, 12, 31)).iso_week == "2026-W01"
    # 月ラベルのほうはカレンダー月のまま。
    assert cohort_of(jst(2025, 12, 31)).month == "2025-12"


def test_naive_datetime_is_refused() -> None:
    from datetime import datetime

    with pytest.raises(ValueError):
        cohort_of(datetime(2026, 8, 11, 12, 0))  # noqa: DTZ001 - 意図的に naive


def test_recent_cohort_is_the_one_still_in_progress() -> None:
    now = jst(2026, 8, 11)
    this_week = cohort_of(now)
    last_week = cohort_of(jst(2026, 8, 4))

    assert is_recent_cohort(this_week, now) is True
    assert is_recent_cohort(last_week, now) is False


def test_recency_respects_granularity() -> None:
    """月次の表では「先週」も同じ月なので直近扱いになる。"""
    now = jst(2026, 8, 11)
    last_week = cohort_of(jst(2026, 8, 4))

    assert is_recent_cohort(last_week, now, Granularity.WEEKLY) is False
    assert is_recent_cohort(last_week, now, Granularity.MONTHLY) is True
