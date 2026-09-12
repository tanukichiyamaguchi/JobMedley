"""Gates that read the platform's own scout history.

**ここは「こちらの送信記録」ではなく「媒体側の履歴」を見る。** 自動化を始める
前に人手で送った分は ``send_records`` に無く、媒体側にしか無い (実測51回目)。

:mod:`jobmedley_scout.state.recency` が「直近に送ったか」を見るのに対し、ここは
**辞退** を見る。日時を解釈しないので :mod:`datetime` に依存せず、``targeting``
側に置ける。

なぜ別に要るのか
----------------

2026-09-12、本番の送信が ``HTTP 200`` かつ ``data.result.errorMessage`` 有りで
失敗した。原因は媒体の画面でしか分からない形になっていたが、調べる途中で
**``ScoutHistorySummary.refused()`` が誰からも呼ばれていない** ことが分かった。

媒体から ``latestRefusedAt`` を取り込み、モデルに ``refused()`` を書き、
それで終わっていた。**部品ごとには正しく、部品のあいだが繋がっていない** --
このプロジェクトで繰り返している形である (実測48回目・55回目と同じ)。

繋がっていないあいだ、この仕組みは **スカウトを断った相手へ送ろうとする。**
断った人にもう一度送るのは、失敗する送信よりも害が大きい。
"""

from __future__ import annotations

from typing import Final

from jobmedley_scout.models.candidate import ScoutHistorySummary
from jobmedley_scout.targeting.determination import (
    Determination,
    RuleOutcome,
    matched,
    not_matched,
    undeterminable,
)

#: このルールの識別子。``MATCH`` は「辞退されている = 送ってはいけない」である。
RULE_REFUSED_SCOUT: Final[str] = "refused_scout"


def refused_scout(summary: ScoutHistorySummary | None) -> RuleOutcome:
    """Whether the candidate has declined a scout from us.

    三値である。**「観測していない」を「辞退されていない」に畳まない。**
    畳むと、レジュメが読めなかった候補者が全員「断っていない」ことになり、
    断った相手へ送り始める -- 静かに、赤くもならず (原則2)。

    ``latest_refused_at`` は **真偽としてしか使わない。** 書式を1件も観測して
    いないので、日時としては解釈しない (原則3)。埋まっていれば辞退である。
    """
    if summary is None:
        return undeterminable(
            RULE_REFUSED_SCOUT,
            evidence=(
                "媒体側のスカウト履歴を観測できていないため、辞退の有無が分かりません"
                "(レジュメが読めなかった / 座標が未確定)。"
            ),
        )
    if summary.refused():
        return matched(
            RULE_REFUSED_SCOUT,
            evidence=(
                "媒体側の履歴に辞退の記録があります。**この相手には送りません。**"
                "断った相手へもう一度送るのは、送信が失敗するより害が大きい。"
            ),
        )
    return not_matched(
        RULE_REFUSED_SCOUT,
        evidence=f"媒体側の履歴に辞退の記録はありません (履歴 {len(summary.entries)} 件)。",
    )


def should_skip(outcome: RuleOutcome) -> bool:
    """Whether to leave this candidate alone. **判定不能は送らない側へ倒す。**

    取り消せないのは送るほうで、送らないのは次回やり直せる。
    :func:`jobmedley_scout.state.recency.should_skip` と同じ倒し方である。
    """
    return outcome.determination is not Determination.NO_MATCH
