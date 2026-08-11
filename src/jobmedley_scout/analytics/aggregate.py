"""Pure reply-rate tables from send and reply records.

三つの不変条件がここにある。

**9.4 -- 「合計 == 無料 + 有料 + 不明」。** ``SendSlot.UNKNOWN`` は一級市民として
残す。参照実装は分析基盤を後から入れたため送信枠が記録されておらず「内訳不明」が
積み上がったが、そこで不明を無料や有料に寄せていたら、内訳は嘘になったうえに
「記録できていない」という事実そのものが見えなくなっていた。恒等式は
:class:`CohortRow` が構築時に検証する (テストで表明済み)。

**埋められない値を既定値に寄せない。** 送信0件の週の返信率は ``0.0`` ではなく
「計算不能」である。0.0 を入れるとグラフ上は「返信率が落ちた週」に見える。
:class:`ReplyRate` は値と **理由** (:class:`RateStatus`) を一緒に返す。

**分母は候補者数であって送信通数ではない。** 追客を分母に足すと、追客した週ほど
返信率が下がる。コホートも枠も **初回送信** のものを使う (11.3)。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from jobmedley_scout.analytics.cohort import Cohort, Granularity, cohort_of, is_recent_cohort
from jobmedley_scout.config.schema import AnalyticsConfig
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.reply import ReplyDetection
from jobmedley_scout.models.send_record import SendRecord, SendSlot, SendStatus

#: 内訳を出す順序。**明示列挙する** (``tuple(SendSlot)`` にしない)。
#: 新しい枠が媒体側に増えたとき、ここに足す作業が「表示列も足す」作業
#: (:mod:`analytics.sheet_schema`) と対になる。自動生成にすると、列が無いまま
#: 集計だけ増えて恒等式が静かに崩れる (9.4)。網羅性はテストで表明する。
SLOT_ORDER: Final[tuple[SendSlot, ...]] = (SendSlot.FREE, SendSlot.PAID, SendSlot.UNKNOWN)

#: 傾きを「横ばい」とみなす幅 (返信率の絶対差)。0.5ポイント未満は誤差とみなす。
FLAT_EPSILON: Final[float] = 0.005


class RateStatus(StrEnum):
    """Why a reply rate has (or has not) a value.

    ``NO_SAMPLE`` と ``SUPPRESSED`` を区別するのは、前者が「まだ送っていない」、
    後者が「送ったが母数が足りず数字を出さない」という別の事実だから。
    どちらも ``value is None`` だが、運用者への意味が違う。
    """

    COMPUTED = "computed"
    NO_SAMPLE = "no_sample"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class ReplyRate:
    """A reply rate together with the reason it does or does not exist."""

    status: RateStatus
    #: ``None`` unless ``status is RateStatus.COMPUTED``. 既定値に寄せないこと。
    value: float | None
    sample: int
    min_sample: int

    @staticmethod
    def of(replies: int, sent: int, min_sample: int) -> ReplyRate:
        if sent <= 0:
            # 送信0件の週の返信率は 0% ではなく「計算不能」。0.0 を入れると
            # グラフ上「返信率が落ちた週」に見え、実際には送っていないだけ。
            return ReplyRate(RateStatus.NO_SAMPLE, None, sent, min_sample)
        if sent < min_sample:
            # 母数が少ないと1件の返信で率が跳ねる。ノイズを出すより出さない。
            return ReplyRate(RateStatus.SUPPRESSED, None, sent, min_sample)
        return ReplyRate(RateStatus.COMPUTED, replies / sent, sent, min_sample)


@dataclass(frozen=True)
class SlotBreakdown:
    """One send-slot's share of a cohort."""

    slot: SendSlot
    sent: int
    replies: int
    reply_rate: ReplyRate


@dataclass(frozen=True)
class FirstSend:
    """A candidate's first successful send -- the row that defines their cohort."""

    candidate_id: str
    sent_at: datetime
    slot: SendSlot
    cohort: Cohort


@dataclass(frozen=True)
class CohortRow:
    """One period's reply rate, with the per-slot breakdown."""

    #: バケット内の代表値。**``granularity`` と対応する側のフィールドだけが
    #: 意味を持つ。** 週は月をまたぐ (2026-W31 は7/27〜8/2) ので、週次の行で
    #: ``cohort.month`` を読むと、その週のどの送信を代表に選んだかで値が変わる。
    #: 行を識別する値が要るときは常に :attr:`key` を使うこと。
    cohort: Cohort
    key: str
    granularity: Granularity
    sent: int
    replies: int
    reply_rate: ReplyRate
    by_slot: tuple[SlotBreakdown, ...]
    #: この期間はまだ返信が届き得る (11.3)。描画側が注記を出すために使う。
    is_recent: bool

    def __post_init__(self) -> None:
        # 9.4: 「合計 == 無料 + 有料 + 不明」を **構築時に** 検証する。恒等式が
        # 壊れる形の変更 (不明を既定値に寄せる、枠を1つ落とす) をここで止める。
        # テストだけに任せると、集計側の分岐が増えたときに黙って崩れる。
        seen = tuple(breakdown.slot for breakdown in self.by_slot)
        if set(seen) != set(SLOT_ORDER) or len(seen) != len(SLOT_ORDER):
            raise ValueError(f"送信枠の内訳が SLOT_ORDER と一致しません: {seen}")
        if sum(breakdown.sent for breakdown in self.by_slot) != self.sent:
            raise ValueError(f"合計送信数 {self.sent} が枠別の合計と一致しません (9.4)")
        if sum(breakdown.replies for breakdown in self.by_slot) != self.replies:
            raise ValueError(f"合計返信数 {self.replies} が枠別の合計と一致しません (9.4)")

    def slot(self, slot: SendSlot) -> SlotBreakdown:
        """The breakdown for one slot. Always present, even when zero."""
        for breakdown in self.by_slot:
            if breakdown.slot is slot:
                return breakdown
        raise ValueError(f"未知の送信枠です: {slot}")  # pragma: no cover - __post_init__ が先


@dataclass(frozen=True)
class CohortTable:
    """A full table plus the things it could *not* account for.

    帰属できなかった返信を戻り値に出すのは、**黙って落とすと分子だけが減って
    返信率が下がるから**。「対応する初回送信が見つからない返信」は移行データや
    手動送信の痕跡であり、運用者が気づくべき事実である。返信日の週に寄せて
    帳尻を合わせてはならない (11.3)。
    """

    granularity: Granularity
    rows: tuple[CohortRow, ...]
    #: 初回送信が特定できなかった返信の候補者ID。
    unattributed_candidate_ids: tuple[str, ...]
    #: ``SENT`` なのに送信時刻が無い候補者ID (データ不整合)。
    sent_without_timestamp_candidate_ids: tuple[str, ...]
    #: 表示期間の外に落ちたコホート数。
    truncated_cohorts: int


class TrendDirection(StrEnum):
    """Which way the reply rate is moving."""

    UP = "up"
    DOWN = "down"
    FLAT = "flat"
    #: 比較できる期間が2つ未満。「変化なし」ではない。
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class TrendPoint:
    key: str
    sent: int
    replies: int
    reply_rate: ReplyRate
    is_recent: bool


@dataclass(frozen=True)
class Trend:
    """The recent series plus a direction derived only from comparable points."""

    granularity: Granularity
    points: tuple[TrendPoint, ...]
    direction: TrendDirection
    #: 最古と最新の比較可能な点の差。判定不能なら ``None``。
    delta: float | None
    min_sample: int
    #: 傾き判定に使えた点の数。
    comparable_points: int


def _replied_candidate_ids(replies: Iterable[ReplyDetection]) -> frozenset[str]:
    """The candidates with at least one detected reply.

    同じ候補者に初回と追客の両方が当たっても1人として数える。分母が候補者数
    なので、分子も候補者数でなければ返信率が100%を超えうる。
    """
    return frozenset(detection.candidate_id for detection in replies)


def _index_first_sends(
    sends: Iterable[SendRecord],
) -> tuple[tuple[FirstSend, ...], tuple[str, ...]]:
    """Each candidate's first successful send, plus the records we could not place."""
    earliest: dict[str, SendRecord] = {}
    missing_timestamp: set[str] = set()
    for record in sends:
        # 実際に送られたものだけを分母に入れる。GENERATED / SKIPPED / FAILED は
        # 送信されていないので、分母に入れると返信率が構造的に下がる。
        # SENDING も除く -- 送ったか不明な状態であり、確定した送信ではない (9.2)。
        if record.status is not SendStatus.SENT:
            continue
        if record.sent_at is None:
            # SENT なのに時刻が無いのはデータ不整合。今の時刻を代入して埋めると
            # 過去の送信が今週のコホートに混ざるので、寄せずに報告する。
            missing_timestamp.add(record.candidate_id)
            continue
        current = earliest.get(record.candidate_id)
        if current is None or _is_earlier(record, current):
            earliest[record.candidate_id] = record

    firsts = tuple(
        FirstSend(
            candidate_id=record.candidate_id,
            sent_at=record.sent_at,
            slot=record.slot,
            cohort=cohort_of(record.sent_at),
        )
        for record in sorted(earliest.values(), key=lambda r: (r.candidate_id, r.record_id))
        if record.sent_at is not None  # mypy 用。上のループで None は除外済み。
    )
    return firsts, tuple(sorted(missing_timestamp - set(earliest)))


def _is_earlier(candidate: SendRecord, current: SendRecord) -> bool:
    """Strict ordering with a deterministic tie-break.

    同時刻の送信が2件あるとき ``record_id`` で決めないと、入力順で枠の内訳が
    変わる = 同じ入力から違う表が出る。全量書き換えの冪等性が壊れる。
    """
    if candidate.sent_at is None or current.sent_at is None:  # pragma: no cover - 呼び出し側で除外
        return False
    if candidate.sent_at != current.sent_at:
        return candidate.sent_at < current.sent_at
    return candidate.record_id < current.record_id


def _build_row(
    key: str,
    members: tuple[FirstSend, ...],
    replied: frozenset[str],
    granularity: Granularity,
    now: datetime,
    min_sample: int,
) -> CohortRow:
    sent_by_slot: dict[SendSlot, int] = {slot: 0 for slot in SLOT_ORDER}
    replies_by_slot: dict[SendSlot, int] = {slot: 0 for slot in SLOT_ORDER}
    for member in members:
        # 未知の枠は UNKNOWN に落とす。ここで例外にすると、媒体が枠を増やした
        # 瞬間に分析全体が止まる。落としたことは UNKNOWN 列に必ず現れる (9.4)。
        slot = member.slot if member.slot in sent_by_slot else SendSlot.UNKNOWN
        sent_by_slot[slot] += 1
        if member.candidate_id in replied:
            replies_by_slot[slot] += 1

    sent = len(members)
    replies = sum(1 for member in members if member.candidate_id in replied)
    return CohortRow(
        cohort=members[0].cohort,
        key=key,
        granularity=granularity,
        sent=sent,
        replies=replies,
        reply_rate=ReplyRate.of(replies, sent, min_sample),
        by_slot=tuple(
            SlotBreakdown(
                slot=slot,
                sent=sent_by_slot[slot],
                replies=replies_by_slot[slot],
                reply_rate=ReplyRate.of(replies_by_slot[slot], sent_by_slot[slot], min_sample),
            )
            for slot in SLOT_ORDER
        ),
        is_recent=is_recent_cohort(members[0].cohort, now, granularity),
    )


def _table(
    sends: Iterable[SendRecord],
    replies: Iterable[ReplyDetection],
    granularity: Granularity,
    periods: int,
    now: datetime,
    min_sample: int,
) -> CohortTable:
    if periods <= 0:
        # 0 を許すと「表が空」という形で静かに機能が消える (7.6 と同じ形)。
        raise ConfigError(f"表示期間は1以上である必要があります: {periods}")

    firsts, missing_timestamp = _index_first_sends(sends)
    replied = _replied_candidate_ids(replies)

    buckets: dict[str, list[FirstSend]] = {}
    for first in firsts:
        buckets.setdefault(first.cohort.key(granularity), []).append(first)

    keys = sorted(buckets)
    truncated = max(0, len(keys) - periods)
    rows = tuple(
        _build_row(key, tuple(buckets[key]), replied, granularity, now, min_sample)
        for key in keys[truncated:]
    )

    attributed = {first.candidate_id for first in firsts}
    return CohortTable(
        granularity=granularity,
        rows=rows,
        unattributed_candidate_ids=tuple(sorted(replied - attributed)),
        sent_without_timestamp_candidate_ids=missing_timestamp,
        truncated_cohorts=truncated,
    )


def weekly_table(
    sends: Iterable[SendRecord],
    replies: Iterable[ReplyDetection],
    cfg: AnalyticsConfig,
    now: datetime,
    *,
    min_sample: int = 0,
) -> CohortTable:
    """Reply rate per ISO week of the candidate's FIRST send (11.3).

    ``min_sample`` の既定は 0 = 抑止しない。表は実測を出す場所であり、母数は
    隣の送信数列に出ている。抑止が要るのは傾き (:func:`trend`) のほう。
    """
    return _table(sends, replies, Granularity.WEEKLY, cfg.weekly_periods, now, min_sample)


def monthly_table(
    sends: Iterable[SendRecord],
    replies: Iterable[ReplyDetection],
    cfg: AnalyticsConfig,
    now: datetime,
    *,
    min_sample: int = 0,
) -> CohortTable:
    """Reply rate per calendar month of the candidate's FIRST send (11.3)."""
    return _table(sends, replies, Granularity.MONTHLY, cfg.monthly_periods, now, min_sample)


def trend(
    sends: Iterable[SendRecord],
    replies: Iterable[ReplyDetection],
    cfg: AnalyticsConfig,
    now: datetime,
    *,
    granularity: Granularity = Granularity.WEEKLY,
) -> Trend:
    """The recent series and its direction, honouring ``trend_min_sample``.

    判定から直近コホートを **必ず除外** する。直近は返信が届き切っていないので
    (11.3)、含めると毎回「悪化」と出る。オオカミ少年になった指標は見られなくなる。
    """
    periods = cfg.trend_weeks if granularity is Granularity.WEEKLY else cfg.trend_months
    table = _table(sends, replies, granularity, periods, now, cfg.trend_min_sample)
    points = tuple(
        TrendPoint(
            key=row.key,
            sent=row.sent,
            replies=row.replies,
            reply_rate=row.reply_rate,
            is_recent=row.is_recent,
        )
        for row in table.rows
    )
    comparable = [
        point
        for point in points
        if point.reply_rate.status is RateStatus.COMPUTED and not point.is_recent
    ]
    if len(comparable) < 2:
        return Trend(
            granularity=granularity,
            points=points,
            direction=TrendDirection.INSUFFICIENT,
            delta=None,
            min_sample=cfg.trend_min_sample,
            comparable_points=len(comparable),
        )

    oldest = comparable[0].reply_rate.value
    newest = comparable[-1].reply_rate.value
    if oldest is None or newest is None:  # pragma: no cover - COMPUTED は値を必ず持つ
        # assert 文で書かない。python -O で消えるうえ、消えた状態では None が
        # そのまま計算に流れ込む。型を絞る目的なら実行時分岐で書くこと。
        raise ValueError("COMPUTED な返信率に値がありません")
    delta = newest - oldest
    if abs(delta) < FLAT_EPSILON:
        direction = TrendDirection.FLAT
    else:
        direction = TrendDirection.UP if delta > 0 else TrendDirection.DOWN
    return Trend(
        granularity=granularity,
        points=points,
        direction=direction,
        delta=delta,
        min_sample=cfg.trend_min_sample,
        comparable_points=len(comparable),
    )


def slot_totals(rows: Iterable[CohortRow]) -> Mapping[SendSlot, SlotBreakdown]:
    """Totals across rows, per slot. Used by the renderer's footer line."""
    sent: dict[SendSlot, int] = {slot: 0 for slot in SLOT_ORDER}
    replies: dict[SendSlot, int] = {slot: 0 for slot in SLOT_ORDER}
    for row in rows:
        for breakdown in row.by_slot:
            sent[breakdown.slot] += breakdown.sent
            replies[breakdown.slot] += breakdown.replies
    return {
        slot: SlotBreakdown(
            slot=slot,
            sent=sent[slot],
            replies=replies[slot],
            reply_rate=ReplyRate.of(replies[slot], sent[slot], 0),
        )
        for slot in SLOT_ORDER
    }
