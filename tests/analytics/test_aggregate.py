"""9.4 の恒等式と 11.3 の帰属。埋められない値を既定値に寄せないことも表明する。"""

from __future__ import annotations

import pytest

from jobmedley_scout.analytics.aggregate import (
    SLOT_ORDER,
    CohortRow,
    RateStatus,
    ReplyRate,
    SlotBreakdown,
    TrendDirection,
    monthly_table,
    slot_totals,
    trend,
    weekly_table,
)
from jobmedley_scout.analytics.cohort import Granularity, cohort_of
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.send_record import MessageKind, SendSlot, SendStatus
from tests.analytics.helpers import jst, make_config, make_reply, make_send


def test_reply_attributes_to_the_first_sends_week_even_when_it_arrives_weeks_later() -> None:
    """11.3 の核心。返信が届いた週ではなく、送った週の返信率が上がる。"""
    sends = [make_send("c1", jst(2026, 7, 6))]  # 2026-W28
    replies = [make_reply("c1", jst(2026, 8, 11))]  # 5週間後 = 2026-W33 に到着

    table = weekly_table(sends, replies, make_config(), jst(2026, 8, 11))
    keys = [row.key for row in table.rows]

    assert keys == ["2026-W28"]
    assert table.rows[0].replies == 1
    assert table.rows[0].reply_rate.value == pytest.approx(1.0)
    # 到着週の行は存在しない。存在したら受信日で数えている。
    assert "2026-W33" not in keys


def test_total_equals_free_plus_paid_plus_unknown() -> None:
    """9.4: 不明を有料/無料に畳まない。恒等式が成り立つ。"""
    sends = [
        make_send("free-1", jst(2026, 8, 4), slot=SendSlot.FREE),
        make_send("free-2", jst(2026, 8, 4), slot=SendSlot.FREE),
        make_send("paid-1", jst(2026, 8, 5), slot=SendSlot.PAID),
        make_send("unk-1", jst(2026, 8, 5), slot=SendSlot.UNKNOWN),
        make_send("unk-2", jst(2026, 8, 6), slot=SendSlot.UNKNOWN),
    ]
    replies = [make_reply("free-1"), make_reply("unk-2")]

    row = weekly_table(sends, replies, make_config(), jst(2026, 8, 11)).rows[0]

    assert row.sent == 5
    assert row.sent == (
        row.slot(SendSlot.FREE).sent
        + row.slot(SendSlot.PAID).sent
        + row.slot(SendSlot.UNKNOWN).sent
    )
    assert row.replies == 2
    assert row.replies == (
        row.slot(SendSlot.FREE).replies
        + row.slot(SendSlot.PAID).replies
        + row.slot(SendSlot.UNKNOWN).replies
    )
    assert row.slot(SendSlot.UNKNOWN).sent == 2


def test_every_slot_appears_even_with_zero_sends() -> None:
    """不明枠の列が「0件だから」と消えると、記録漏れが見えなくなる (9.4)。"""
    sends = [make_send("c1", jst(2026, 8, 4), slot=SendSlot.FREE)]

    row = weekly_table(sends, [], make_config(), jst(2026, 8, 11)).rows[0]

    assert tuple(breakdown.slot for breakdown in row.by_slot) == SLOT_ORDER
    assert row.slot(SendSlot.UNKNOWN).sent == 0
    # 0件の枠の返信率は 0% ではなく算出不能。
    assert row.slot(SendSlot.UNKNOWN).reply_rate.status is RateStatus.NO_SAMPLE
    assert row.slot(SendSlot.UNKNOWN).reply_rate.value is None


def test_slot_order_covers_every_declared_slot() -> None:
    """枠が増えたら SLOT_ORDER に足すこと。足さないと内訳から静かに消える。"""
    assert set(SLOT_ORDER) == set(SendSlot)


def test_broken_identity_is_refused_at_construction() -> None:
    """恒等式は構築時に検査する。テストだけに任せると集計の分岐追加で崩れる。"""
    cohort = cohort_of(jst(2026, 8, 4))
    with pytest.raises(ValueError, match="9.4"):
        CohortRow(
            cohort=cohort,
            key="2026-W32",
            granularity=Granularity.WEEKLY,
            sent=5,  # 内訳の合計は 1 なので不一致
            replies=0,
            reply_rate=ReplyRate.of(0, 5, 0),
            by_slot=tuple(
                SlotBreakdown(
                    slot=slot,
                    sent=1 if slot is SendSlot.FREE else 0,
                    replies=0,
                    reply_rate=ReplyRate.of(0, 0, 0),
                )
                for slot in SLOT_ORDER
            ),
            is_recent=False,
        )


def test_only_sent_records_count_towards_the_denominator() -> None:
    sends = [
        make_send("sent", jst(2026, 8, 4), status=SendStatus.SENT),
        make_send("generated", jst(2026, 8, 4), status=SendStatus.GENERATED),
        make_send("failed", jst(2026, 8, 4), status=SendStatus.FAILED),
        make_send("skipped", jst(2026, 8, 4), status=SendStatus.SKIPPED),
        make_send("sending", jst(2026, 8, 4), status=SendStatus.SENDING),
    ]

    row = weekly_table(sends, [], make_config(), jst(2026, 8, 11)).rows[0]

    assert row.sent == 1


def test_followups_do_not_inflate_the_denominator() -> None:
    """分母は候補者数。追客を数えると、追客した週ほど返信率が下がって見える。"""
    sends = [
        make_send("c1", jst(2026, 8, 4), kind=MessageKind.FIRST_CONTACT, record_id=1),
        make_send("c1", jst(2026, 8, 6), kind=MessageKind.FOLLOW_UP, record_id=2),
    ]
    replies = [make_reply("c1"), make_reply("c1")]  # 同一候補者に2件当たっても1人

    row = weekly_table(sends, replies, make_config(), jst(2026, 8, 11)).rows[0]

    assert row.sent == 1
    assert row.replies == 1
    assert row.reply_rate.value == pytest.approx(1.0)


def test_cohort_and_slot_come_from_the_first_send() -> None:
    """週をまたぐ追客が、候補者を後の週へ移してはならない。"""
    sends = [
        make_send("c1", jst(2026, 8, 4), slot=SendSlot.FREE, record_id=1),
        make_send(
            "c1",
            jst(2026, 8, 18),
            slot=SendSlot.PAID,
            kind=MessageKind.FOLLOW_UP,
            record_id=2,
        ),
    ]

    table = weekly_table(sends, [], make_config(), jst(2026, 8, 25))

    assert [row.key for row in table.rows] == ["2026-W32"]
    assert table.rows[0].slot(SendSlot.FREE).sent == 1
    assert table.rows[0].slot(SendSlot.PAID).sent == 0


def test_zero_send_rate_is_not_defaulted_to_zero_percent() -> None:
    rate = ReplyRate.of(0, 0, 0)

    assert rate.status is RateStatus.NO_SAMPLE
    assert rate.value is None


def test_a_reply_without_a_matching_send_is_reported_not_absorbed() -> None:
    """帳尻合わせに返信日の週へ寄せない。見える所に出す。"""
    sends = [make_send("c1", jst(2026, 8, 4))]
    replies = [make_reply("c1"), make_reply("ghost")]

    table = weekly_table(sends, replies, make_config(), jst(2026, 8, 11))

    assert table.unattributed_candidate_ids == ("ghost",)
    assert table.rows[0].replies == 1


def test_sent_without_timestamp_is_reported_not_dated_to_now() -> None:
    sends = [make_send("broken", None, status=SendStatus.SENT)]

    table = weekly_table(sends, [], make_config(), jst(2026, 8, 11))

    assert table.rows == ()
    assert table.sent_without_timestamp_candidate_ids == ("broken",)


def test_table_is_limited_to_the_configured_number_of_periods() -> None:
    sends = [
        make_send("c1", jst(2026, 7, 6)),
        make_send("c2", jst(2026, 7, 13)),
        make_send("c3", jst(2026, 7, 20)),
    ]

    table = weekly_table(sends, [], make_config(weekly_periods=2), jst(2026, 8, 11))

    assert [row.key for row in table.rows] == ["2026-W29", "2026-W30"]
    assert table.truncated_cohorts == 1


def test_zero_periods_is_refused() -> None:
    """0 を許すと「表が空」という形で機能が静かに消える。"""
    with pytest.raises(ConfigError):
        weekly_table([], [], make_config(weekly_periods=0), jst(2026, 8, 11))


def test_monthly_table_buckets_by_calendar_month() -> None:
    sends = [
        make_send("c1", jst(2026, 7, 31)),
        make_send("c2", jst(2026, 8, 1)),
        make_send("c3", jst(2026, 8, 20)),
    ]

    table = monthly_table(sends, [], make_config(), jst(2026, 8, 25))

    assert [(row.key, row.sent) for row in table.rows] == [("2026-07", 1), ("2026-08", 2)]
    assert table.granularity is Granularity.MONTHLY


def test_trend_suppresses_rates_below_the_minimum_sample() -> None:
    """母数が少ない週の率は出さない。1件の返信で率が跳ねる。"""
    sends = [make_send(f"c{i}", jst(2026, 8, 4)) for i in range(3)]
    replies = [make_reply("c0")]

    result = trend(sends, replies, make_config(trend_min_sample=10), jst(2026, 8, 25))

    assert result.points[0].reply_rate.status is RateStatus.SUPPRESSED
    assert result.points[0].reply_rate.value is None
    # 抑止でも母数そのものは見える。
    assert result.points[0].sent == 3
    assert result.direction is TrendDirection.INSUFFICIENT


def test_trend_direction_ignores_the_still_open_cohort() -> None:
    """直近週を入れると、返信が未着なだけで毎回「悪化」と出る (11.3)。"""
    config = make_config(trend_min_sample=2)
    sends = [make_send(f"a{i}", jst(2026, 7, 27)) for i in range(4)]
    sends += [make_send(f"b{i}", jst(2026, 8, 3)) for i in range(4)]
    # 直近週 (2026-W33) は送信済みだが返信ゼロ。
    sends += [make_send(f"c{i}", jst(2026, 8, 10)) for i in range(4)]
    replies = [make_reply("a0"), make_reply("b0"), make_reply("b1"), make_reply("b2")]

    result = trend(sends, replies, config, jst(2026, 8, 11))

    assert [point.key for point in result.points] == ["2026-W31", "2026-W32", "2026-W33"]
    assert result.points[-1].is_recent is True
    # 直近週の 0% を含めれば DOWN になるが、除外すれば 25% -> 75% の UP。
    assert result.direction is TrendDirection.UP
    assert result.comparable_points == 2


def test_trend_with_a_single_comparable_point_is_insufficient_not_flat() -> None:
    sends = [make_send(f"c{i}", jst(2026, 8, 3)) for i in range(4)]

    result = trend(sends, [], make_config(trend_min_sample=2), jst(2026, 8, 11))

    assert result.direction is TrendDirection.INSUFFICIENT
    assert result.delta is None


def test_slot_totals_preserve_the_identity_across_rows() -> None:
    sends = [
        make_send("c1", jst(2026, 7, 27), slot=SendSlot.FREE),
        make_send("c2", jst(2026, 8, 3), slot=SendSlot.PAID),
        make_send("c3", jst(2026, 8, 3), slot=SendSlot.UNKNOWN),
    ]

    table = weekly_table(sends, [make_reply("c2")], make_config(), jst(2026, 8, 11))
    totals = slot_totals(table.rows)

    assert sum(breakdown.sent for breakdown in totals.values()) == 3
    assert totals[SendSlot.PAID].replies == 1
