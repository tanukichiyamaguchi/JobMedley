"""直近にスカウトした相手を外す。**三値である。**

運用者の要求はこうである。

> 直近3日以内にスカウトを送っている場合はスカウト対象からは外すようにしてください。

これを ``bool`` で書くと 7.1 の事故に戻る。

> **「スキップは、黙って合格させるのと同義」。**

判定に要るのは ``latestSentAt`` だが、**この欄の書式を1件も見ていない。**
2026-08-22 の観測は値を出さない方針だったので、分かっているのは ``<string>``
であることだけである（座標ファイルの注記）。したがって

* 読めた → 日付で判定する
* **読めなかった → 「最近ではない」と読み替えない。** 判定不能として返す

判定不能をどちらへ倒すかは呼び出し側が決める。この要求では **送らない側へ倒す**
のが正しい: 直近に送った相手へ重ねて送るのは取り消せないが、送らないのは次回
やり直せる。ただし **黙って0件にしてはいけない**（原則2）ので、外した人数と
理由は必ず報告に出す。

**なぜ ``state`` に居るのか。** 最初 ``targeting`` へ置いて、純粋性の検査に
落とされた -- あちらは ``datetime`` の import 自体を禁じている。この関数は
``now`` を引数で受け取るので壁時計は読まないが、**検査を緩めるのではなく置き場所
を直した**。``state`` 側の検査は「import は型のために許し、``.now()`` の呼び出しを
禁じる」というより正確な形で、こちらの性質と一致する。隣の ``dedupe`` と同じ
「もう一度送ってよいか」を答える家族でもある。

**書式を推測して決め打ちにしない。** 寛容に読んで、読めなければ読めないと言う
のが 7.7（パースは寛容に、破壊は厳格に）である。読めた書式は実測として記録し、
報告に出す -- 1回走らせれば本当の書式が分かる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from jobmedley_scout.models.candidate import ScoutHistorySummary
from jobmedley_scout.targeting.determination import (
    Determination,
    RuleOutcome,
    matched,
    not_matched,
    undeterminable,
)

RULE_RECENTLY_SCOUTED: Final = "recently_scouted"

#: 日付だけの書式。**時刻が無いので、その日の 00:00 として読む。**
#: 画面には「送信日:2025/05/28」と出ているので、この形も来うる。
_DATE_ONLY_FORMATS: Final[tuple[str, ...]] = (
    "%Y/%m/%d",
    "%Y-%m-%d",
    "%Y年%m月%d日",
)

#: 日時の書式。**``fromisoformat`` が読めない形だけを並べる。**
#:
#: 2026-09-12 実測54回目、段階5 の通しで **5名全員が「読めない書式 (長さ 19)」**
#: で外れた。長さ19 の ISO 形 (``2025-05-28T10:00:00`` / ``2025-05-28 10:00:00``)
#: は ``fromisoformat`` が読むので、実際の書式はそのどちらでもない。画面には
#: ``送信日:2025/05/28`` と出ているので、**スラッシュ区切りが有力** である。
#:
#: **並べたのは仮説であって観測ではない。** 当たったかどうかは次の実行で分かる
#: -- 外れていれば :func:`describe_format` が書式そのものを (値抜きで) 出す。
_DATETIME_FORMATS: Final[tuple[str, ...]] = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
)


def parse_sent_at(raw: str | None) -> datetime | None:
    """Read ``latestSentAt`` leniently. ``None`` when it cannot be read.

    **書式は観測していない。** だから決め打ちせず、ありうる形を順に試す。
    どれにも当たらなければ ``None`` を返す -- そこで「最近ではない」と
    みなさないことがこの関数の役目である。

    返り値は必ず **タイムゾーン付き** にする。素朴な datetime と付きの
    datetime は比較すると ``TypeError`` になるので、判定側で落ちる。
    タイムゾーンが書かれていなければ UTC とみなす（そう *みなした* ことは
    :func:`describe_format` が報告に出す）。
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    # ISO8601。``Z`` は 3.11 の fromisoformat が読めないので置き換える。
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    for fmt in (*_DATETIME_FORMATS, *_DATE_ONLY_FORMATS):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


#: 書式を写すときに **そのまま残す** 文字。構造を表すだけで、値を持たない。
_STRUCTURAL: Final[frozenset[str]] = frozenset("TZ")


def shape_of(text: str) -> str:
    """The timestamp's shape, with **every value character masked**.

    数字を ``N``、``T``/``Z`` 以外の英字を ``A`` に置き換え、区切りはそのまま残す。
    ``2025/05/28 10:00:00`` は ``NNNN/NN/NN NN:NN:NN`` になる。

    **これは値ではない。** どの日付かは一切分からず、分かるのは並びだけである。
    13.2 が守りたいのは候補者を特定しうる情報であって、区切り記号の並びではない。

    長さだけでは足りないことが実測54回目で分かった。「読めない書式 (長さ 19)」
    では候補が複数残り、**どれなのかを当てるしかなくなる**。当てるくらいなら
    形を出せばよい -- 値を出さずに形を出す方法はある。
    """
    out: list[str] = []
    for char in text:
        if char.isdigit():
            out.append("N")
        elif char in _STRUCTURAL:
            out.append(char)
        elif char.isalpha():
            out.append("A")
        else:
            out.append(char)
    return "".join(out)


def describe_format(raw: str | None) -> str:
    """What shape the timestamp had. **値そのものは返さない** (13.2)。

    1回走らせれば本当の書式が分かるようにするための報告用。返すのは
    :func:`shape_of` が作る **値を伏せた並び** と、読めたかどうかである。
    """
    if raw is None:
        return "欄が無い"
    text = raw.strip()
    if not text:
        return "空文字"
    shape = shape_of(text)
    parsed = parse_sent_at(text)
    if parsed is None:
        # **形を出す。** これが無いと、次の実行でも当て推量が続く。
        return f"読めない書式: {shape}"
    if parsed.hour or parsed.minute or parsed.second:
        return f"日時 (時刻あり): {shape}"
    return f"日付のみ (時刻なし): {shape}"


def latest_sent(summary: ScoutHistorySummary) -> tuple[datetime | None, int, int]:
    """The newest readable send time, plus how many were readable / unreadable.

    **読めた件数と読めなかった件数を返す。** 「最新が読めた」だけでは足りない --
    読めなかった行の中にもっと新しい送信があったかもしれず、それは
    「最近ではない」の根拠にならない。
    """
    newest: datetime | None = None
    readable = 0
    unreadable = 0
    for entry in summary.entries:
        parsed = parse_sent_at(entry.latest_sent_at)
        if parsed is None:
            unreadable += 1
            continue
        readable += 1
        if newest is None or parsed > newest:
            newest = parsed
    return newest, readable, unreadable


def scouted_within(
    summary: ScoutHistorySummary | None,
    *,
    now: datetime,
    days: int,
) -> RuleOutcome:
    """``MATCH`` when this candidate was scouted within the last ``days`` days.

    **``MATCH`` は「最近スカウト済み」= 除外対象** を意味する。ここは除外系の
    ルールなので、該当した場合に ``MATCH`` を返す（:class:`Determination` の
    docstring が断っているとおり、``MATCH`` は常に「このルールを満たす」であって
    「送ってよい」ではない）。

    三値の割り当て:

    * ``summary is None``          -- **観測していない。** UNDETERMINABLE
    * 履歴が空                      -- 観測して履歴なし。NO_MATCH（送ってよい）
    * 最新の送信が ``days`` 日以内  -- MATCH（外す）
    * 最新の送信がそれより前        -- NO_MATCH（送ってよい）
    * 日時がどれも読めない          -- UNDETERMINABLE

    **読めない行が1行でも残っていて、読めた最新が範囲外のときも UNDETERMINABLE**
    にする。読めなかった行にもっと新しい送信が在りえて、「範囲外だった」と言い
    切れないからである。ここを「読めた分だけで判定」にすると、読めない書式が
    来た瞬間に静かに送り始める。
    """
    if days < 0:
        raise ValueError(f"days は 0 以上である必要があります: {days}")
    if summary is None:
        return undeterminable(
            RULE_RECENTLY_SCOUTED,
            evidence="媒体のスカウト履歴を観測していません (レジュメ未取得 / 座標未確定)",
        )
    if not summary.entries:
        return not_matched(
            RULE_RECENTLY_SCOUTED,
            evidence="媒体のスカウト履歴は空でした (この求人からは未送信)",
        )

    newest, readable, unreadable = latest_sent(summary)
    cutoff = now - timedelta(days=days)

    if newest is not None and newest >= cutoff:
        # **読めない行が残っていても結論は変わらない。** 既に範囲内の送信が
        # 見つかっているので、外す。
        return matched(
            RULE_RECENTLY_SCOUTED,
            evidence=f"直近 {days} 日以内に送信済み (履歴 {len(summary.entries)} 件)",
            matched_values=(_day_label(newest),),
        )
    if unreadable:
        return undeterminable(
            RULE_RECENTLY_SCOUTED,
            evidence=(
                f"送信日時を読めない履歴が {unreadable} 件あります"
                f" (読めた {readable} 件はいずれも {days} 日より前)。"
                " 読めない行に新しい送信が在りうるので、範囲外とは言い切れません"
            ),
        )
    if newest is None:
        return undeterminable(
            RULE_RECENTLY_SCOUTED,
            evidence=f"履歴 {len(summary.entries)} 件のいずれにも送信日時がありません",
        )
    return not_matched(
        RULE_RECENTLY_SCOUTED,
        evidence=f"最後の送信は {days} 日より前です (履歴 {len(summary.entries)} 件)",
    )


def _day_label(moment: datetime) -> str:
    """The day, as a label for the outcome. **時刻は落とす。**

    ``matched_values`` は「実際に条件を満たした値」を入れる欄で、下流が根拠として
    使う (8.3 対策2)。日そのものは残すが、秒まで積む必要は無い。
    """
    return moment.astimezone(UTC).date().isoformat()


def should_skip(outcome: RuleOutcome) -> bool:
    """Whether to leave this candidate alone, **falling safe when undeterminable**.

    判定不能を「送ってよい」へ倒さないことがこの関数の全部である。直近に送った
    相手へ重ねて送るのは取り消せず、送らないのは次回やり直せる。

    **黙って倒さない。** 呼び出し側は ``outcome.evidence`` を必ず報告へ出すこと --
    全員がここで外れて送信0件になったとき、理由が出ていなければそれは
    「静かなゼロ件」そのものである (原則2)。
    """
    return outcome.determination in (Determination.MATCH, Determination.UNDETERMINABLE)


__all__ = [
    "RULE_RECENTLY_SCOUTED",
    "describe_format",
    "shape_of",
    "latest_sent",
    "parse_sent_at",
    "scouted_within",
    "should_skip",
]
