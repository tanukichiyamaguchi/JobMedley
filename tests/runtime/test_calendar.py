"""12.4: 「土日祝は送らない」をテストできる場所に置く。

参照実装ではこれがシェルスクリプトに書かれており、**祝日を扱えず、テストも
できなかった**。判定は JST で行い、休止の理由を文字列で返す。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobmedley_scout.config.schema import CalendarConfig
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.runtime.calendar import (
    FAILSAFE_RUN_LINE,
    RunDecision,
    parse_protocol,
    render_protocol,
    should_run,
)


def _cfg(
    *,
    skip_weekends: bool = True,
    holidays: tuple[str, ...] = (),
    start: int = 9,
    end: int = 19,
) -> CalendarConfig:
    return CalendarConfig(
        skip_weekends=skip_weekends,
        holidays=holidays,
        send_window_start_hour_jst=start,
        send_window_end_hour_jst=end,
    )


def _jst(year: int, month: int, day: int, hour: int) -> datetime:
    """A UTC instant that is ``hour`` o'clock JST on the given JST date."""
    # JST = UTC+9。テストが「JSTでこの時刻」と読めるように逆算して渡す。
    return datetime(year, month, day, hour, tzinfo=UTC) - _NINE_HOURS


_NINE_HOURS = datetime(2000, 1, 1, 9, tzinfo=UTC) - datetime(2000, 1, 1, 0, tzinfo=UTC)

# 2026-08-11 は火曜、08-15 は土曜、08-16 は日曜、09-21 は月曜。
TUESDAY = (2026, 8, 11)
SATURDAY = (2026, 8, 15)
SUNDAY = (2026, 8, 16)
MONDAY_HOLIDAY = (2026, 9, 21)


def test_weekday_inside_the_window_runs() -> None:
    decision = should_run(_jst(*TUESDAY, 10), _cfg())

    assert decision.should_run is True
    assert decision.reason


def test_weekend_is_skipped_with_a_reason() -> None:
    saturday = should_run(_jst(*SATURDAY, 10), _cfg())
    sunday = should_run(_jst(*SUNDAY, 10), _cfg())

    assert saturday.should_run is False
    assert "土" in saturday.reason
    assert sunday.should_run is False
    assert "日" in sunday.reason


def test_weekend_runs_when_skip_weekends_is_off() -> None:
    """設定で明示的に切れること。既定値ではなく設定が効いていることの確認。"""
    decision = should_run(_jst(*SATURDAY, 10), _cfg(skip_weekends=False))

    assert decision.should_run is True


def test_holiday_is_skipped_and_the_date_is_in_the_reason() -> None:
    """祝日はシェルスクリプトでは表現できなかったもの。理由に日付を出す。"""
    decision = should_run(_jst(*MONDAY_HOLIDAY, 10), _cfg(holidays=("2026-09-21",)))

    assert decision.should_run is False
    assert "2026-09-21" in decision.reason


def test_a_day_not_in_the_holiday_list_still_runs() -> None:
    decision = should_run(_jst(*TUESDAY, 10), _cfg(holidays=("2026-09-21",)))

    assert decision.should_run is True


def test_before_the_window_is_skipped() -> None:
    decision = should_run(_jst(*TUESDAY, 8), _cfg(start=9, end=19))

    assert decision.should_run is False
    assert decision.reason


def test_window_end_is_exclusive() -> None:
    """終端は排他。19時ちょうどは窓の外。"""
    assert should_run(_jst(*TUESDAY, 18), _cfg(start=9, end=19)).should_run is True
    assert should_run(_jst(*TUESDAY, 19), _cfg(start=9, end=19)).should_run is False


def test_window_may_wrap_past_midnight() -> None:
    """22時〜6時のような窓も黙って「常に時間外」にしない。"""
    night = _cfg(skip_weekends=False, start=22, end=6)

    assert should_run(_jst(*TUESDAY, 23), night).should_run is True
    assert should_run(_jst(*TUESDAY, 3), night).should_run is True
    assert should_run(_jst(*TUESDAY, 12), night).should_run is False


def test_evaluated_in_jst_not_utc() -> None:
    """UTCで曜日を切ると、日本の週末判定が9時間ずれる。

    2026-08-14 15:30 UTC は UTC では金曜だが、JST では土曜 00:30 である。
    """
    instant = datetime(2026, 8, 14, 15, 30, tzinfo=UTC)

    decision = should_run(instant, _cfg())

    assert decision.should_run is False
    assert "2026-08-15" in decision.reason


@pytest.mark.parametrize(
    "instant",
    [
        _jst(*TUESDAY, 10),  # 実行
        _jst(*SATURDAY, 10),  # 週末
        _jst(*TUESDAY, 3),  # 時間外
    ],
)
def test_every_decision_carries_a_non_empty_reason(instant: datetime) -> None:
    """理由はログに出る。空だと「なぜか送られていない」が最も厄介な形で残る。"""
    decision = should_run(instant, _cfg(holidays=("2026-09-21",)))

    assert decision.reason.strip()


def test_holiday_decision_also_carries_a_reason() -> None:
    decision = should_run(_jst(*MONDAY_HOLIDAY, 10), _cfg(holidays=("2026-09-21",)))

    assert decision.reason.strip()


def test_a_decision_without_a_reason_is_rejected() -> None:
    with pytest.raises(ValueError, match="理由"):
        RunDecision(should_run=False, reason="   ")


# --- 文字列プロトコル -------------------------------------------------------
def test_protocol_prefixes() -> None:
    """実行基盤は接頭辞だけを見る。ワークフローの ``== SKIP:*`` と同じ形。"""
    assert render_protocol(RunDecision(True, "営業日です")).startswith("RUN:")
    assert render_protocol(RunDecision(False, "祝日です")).startswith("SKIP:")


@pytest.mark.parametrize(
    "decision",
    [
        RunDecision(True, "営業日・時間帯内です"),
        RunDecision(False, "2026-09-21 は休業日として設定されているため送信しません"),
    ],
)
def test_protocol_round_trips(decision: RunDecision) -> None:
    assert parse_protocol(render_protocol(decision)) == decision


def test_real_decisions_round_trip_through_the_protocol() -> None:
    for instant in (_jst(*TUESDAY, 10), _jst(*SATURDAY, 10), _jst(*TUESDAY, 3)):
        decision = should_run(instant, _cfg())
        assert parse_protocol(render_protocol(decision)) == decision


def test_failsafe_line_is_understood_as_run() -> None:
    """判定が落ちたときにワークフローが出す行。休止扱いにしてはならない。"""
    parsed = parse_protocol(FAILSAFE_RUN_LINE)

    assert parsed.should_run is True
    assert parsed.reason


def test_unparseable_line_fails_safe_towards_running() -> None:
    """フェイルセーフの向き: 判定不能で業務を止めない (設定エラーで止めないため)。"""
    parsed = parse_protocol("何かがおかしい")

    assert parsed.should_run is True
    assert parsed.reason


# --- 設定不正 ---------------------------------------------------------------
def test_malformed_holiday_is_not_silently_ignored() -> None:
    """黙って無視すると「祝日を書いたのに送信された」に戻る (12.4)。"""
    with pytest.raises(ConfigError, match="holidays"):
        should_run(_jst(*TUESDAY, 10), _cfg(holidays=("2026/09/21",)))


def test_identical_window_bounds_are_rejected() -> None:
    """24時間なのか0時間なのか、設定から読み取れると言い切れない。"""
    with pytest.raises(ConfigError):
        should_run(_jst(*TUESDAY, 10), _cfg(start=9, end=9))


def test_out_of_range_window_bounds_are_rejected() -> None:
    with pytest.raises(ConfigError):
        should_run(_jst(*TUESDAY, 10), _cfg(start=25, end=26))


def test_naive_datetime_is_rejected() -> None:
    """素の datetime を渡せてしまうと、どのタイムゾーンで切ったか分からなくなる。"""
    with pytest.raises(ValueError, match="naive"):
        should_run(datetime(2026, 8, 11, 10), _cfg())  # noqa: DTZ001
