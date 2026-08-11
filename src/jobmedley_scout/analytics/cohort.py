"""Cohort attribution (11.3).

**返信は「初回送信」の週・月に帰属させる。返信が届いた日の週ではない。**

参照実装は返信を受信日で数えていた。返信は送信から数日〜数週間遅れて届くので、
受信日で数えると、送信を止めた週の返信率が跳ね上がり、大量に送った週の返信率が
低く出る。「どの週の送信が効いたか」を知りたいのに、指標は「どの週に返信が届いたか」
を答えていた。改善判断の材料としては無意味どころか有害である。

境界は JST で切る (:func:`jobmedley_scout.clock.to_jst`)。UTC で切ると週境界が
9時間ずれ、月曜早朝の送信が前週に落ちる。

直近のコホートは、**まだ返信が届き切っていない** ため必ず低く出る。
:func:`is_recent_cohort` はその注記を描画側が出せるようにするためにある。
これを付け忘れると、運用者は毎週「今週は急に悪化した」と誤読する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from jobmedley_scout.clock import to_jst


class Granularity(StrEnum):
    """The period a table or trend is bucketed by."""

    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class Cohort:
    """The period a candidate's *first* send falls into.

    週と月の両方を保持する。同じ初回送信から両方の表を作るので、粒度ごとに
    別々の帰属計算を書くと、片方だけ改修されて表同士が食い違う。
    """

    #: ISO-8601 week label, e.g. ``2026-W33``.
    iso_week: str
    #: Calendar month label, e.g. ``2026-08``.
    month: str

    def key(self, granularity: Granularity) -> str:
        """The bucket key for the given granularity."""
        return self.iso_week if granularity is Granularity.WEEKLY else self.month


def cohort_of(first_sent_at: datetime) -> Cohort:
    """The cohort a candidate belongs to, from the instant of their FIRST send.

    Raises ``ValueError`` for naive datetimes (via :func:`to_jst`).
    """
    local = to_jst(first_sent_at)
    iso = local.isocalendar()
    # ISO週の「年」はカレンダー年と一致しないことがある (12/29〜31 が翌年の W01 に
    # なる、1/1 が前年の W52/W53 になる)。ラベルに local.year を使うと、年末年始の
    # 2週間だけ別のコホートに混ざる。必ず isocalendar().year を使うこと。
    return Cohort(
        iso_week=f"{iso.year}-W{iso.week:02d}",
        month=f"{local.year}-{local.month:02d}",
    )


def is_recent_cohort(
    cohort: Cohort,
    now: datetime,
    granularity: Granularity = Granularity.WEEKLY,
) -> bool:
    """Whether ``cohort`` is the period still in progress at ``now``.

    直近週・直近月は返信がまだ届き得るため、返信率が構造的に低く出る。描画側は
    この結果で注記を出し、傾き判定 (:func:`aggregate.trend`) はこの期間を除外する。
    """
    return cohort.key(granularity) == cohort_of(now).key(granularity)
