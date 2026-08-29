"""求人IDを応答から取り出す。**純粋** — ブラウザもネットワークも要らない。

送信 payload に残っている ``<...>`` は4つあり、そのうち2つは **こちらが先に
決めておく値** である::

    variables.input.jobOfferId        <- どの求人へのスカウトか
    variables.input.jobOfferSalaryId  <- その求人のどの給与条件か
    variables.input.memberId          <- 実行時に一覧から取る
    variables.input.searchUuid        <- 実行時に一覧から取る

前の2つは運用者が「ヤガサキ歯科医院 歯科衛生士 正職員」と指定済みだが、
**IDそのものは画面にもプロンプトにも出てこない。** 一覧を開いたときに飛ぶ
``GET /api/customers/job_offers/published/`` の応答にだけ入っている。

**この応答に候補者の個人データは無い。** 返るのは運用者自身が媒体へ公開して
いる求人票であり、13.2 が守ろうとしている対象 (氏名・会員番号・年齢・居住地)
とは別物である。だから **ここでは値を出す。** 出さなければ、どの ID がどの
求人なのか運用者が突き合わせられず、座標を埋める手段が無くなる。

**ただし出す範囲は絞る。** 求人オブジェクトの直下にある短い文字列だけを見出しに
使い、入れ子は辿らない。辿ると、この endpoint が将来なにを抱えるか分からない
まま全部を印字することになる。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

#: 求人の並びが入っているキーの名前。**位置は固定しない。**
#:
#: ``data.job_offers[]`` で観測したが、封筒の形が変わっても壊れないよう
#: 名前で探す。見つからなければ「見つからなかった」と報告する (推測しない)。
JOB_OFFERS_KEY: Final[str] = "job_offers"

#: 給与条件の並びが入っているキーの名前。
SALARIES_KEY: Final[str] = "job_offer_salaries"

#: 見出しに使う文字列の長さの上限。これを超える欄は本文 (説明文) とみなして出さない。
MAX_LABEL_CHARS: Final[int] = 80

#: 封筒を潜る深さの上限。暴走止めであって媒体の事実ではない。
MAX_DEPTH: Final[int] = 6


@dataclass(frozen=True)
class JobOffer:
    """One published job offer, reduced to what the send payload needs."""

    offer_id: str
    salary_ids: tuple[str, ...]
    #: 見出しに使う ``(キー名, 値)``。求人オブジェクトの直下の短い文字列だけ。
    labels: tuple[tuple[str, str], ...]

    def render(self) -> str:
        head = " / ".join(f"{k}={v}" for k, v in self.labels) or "(見出しになる欄がありません)"
        salaries = ", ".join(self.salary_ids) or "(給与条件が空)"
        return f"  jobOfferId={self.offer_id}\n    {head}\n    jobOfferSalaryId: {salaries}"


def _as_id(value: object) -> str | None:
    """IDらしい値を文字列にする。**それ以外は None** (推測で拾わない)。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _labels(offer: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    """Short direct string fields, for the operator to tell offers apart.

    **入れ子は辿らない。** 辿ると、この endpoint が将来なにを抱えるか分からない
    まま全部を印字することになる。
    """
    out: list[tuple[str, str]] = []
    for key, value in offer.items():
        if not isinstance(key, str) or key == "id":
            continue
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text and len(text) <= MAX_LABEL_CHARS:
            out.append((key, text))
    return tuple(out)


def _salary_ids(offer: Mapping[str, object]) -> tuple[str, ...]:
    raw = offer.get(SALARIES_KEY)
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    found: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        got = _as_id(item.get("id"))
        if got is not None:
            found.append(got)
    return tuple(found)


def _find_offer_array(node: object, depth: int = MAX_DEPTH) -> Sequence[object] | None:
    """Locate the ``job_offers`` array anywhere in the envelope. **名前で探す。**"""
    if depth <= 0:
        return None
    if isinstance(node, Mapping):
        found = node.get(JOB_OFFERS_KEY)
        if isinstance(found, Sequence) and not isinstance(found, str | bytes):
            return found
        for value in node.values():
            deeper = _find_offer_array(value, depth - 1)
            if deeper is not None:
                return deeper
        return None
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        for item in node:
            deeper = _find_offer_array(item, depth - 1)
            if deeper is not None:
                return deeper
    return None


def extract_job_offers(body: object) -> tuple[JobOffer, ...]:
    """Pull every published job offer out of the response. **Pure.**

    ``id`` の読めない要素は **落とす**。ID が無ければ送信 payload に使えず、
    並びに残しておくと「候補はあったが選べない」という報告になる。
    """
    array = _find_offer_array(body)
    if array is None:
        return ()
    offers: list[JobOffer] = []
    for item in array:
        if not isinstance(item, Mapping):
            continue
        offer_id = _as_id(item.get("id"))
        if offer_id is None:
            continue
        offers.append(
            JobOffer(
                offer_id=offer_id,
                salary_ids=_salary_ids(item),
                labels=_labels(item),
            )
        )
    return tuple(offers)


def render_offers(offers: Sequence[JobOffer], *, wanted: str) -> str:
    """The operator-facing report. **0件でも黙らない** (原則2)."""
    lines = ["求人IDの候補 (**運用者自身の公開求人です。候補者の情報ではありません**)", ""]
    if not offers:
        lines.append("  **1件も取り出せませんでした。**")
        lines.append(f"  応答に '{JOB_OFFERS_KEY}' の並びが見つからないか、id が読めませんでした。")
        lines.append("  → 「求人が無い」のか「探し方が違う」のかは、この報告からは決まりません。")
        return "\n".join(lines)

    lines.append(f"  取り出せた求人: {len(offers)} 件")
    lines.append("")
    for offer in offers:
        lines.append(offer.render())
        lines.append("")

    matched = [o for o in offers if any(wanted in value for _, value in o.labels)]
    lines.append(f"探している求人: {wanted}")
    if len(matched) == 1:
        only = matched[0]
        lines.append(f"  **1件に絞れました。** jobOfferId={only.offer_id}")
        if len(only.salary_ids) == 1:
            lines.append(f"  jobOfferSalaryId={only.salary_ids[0]} (給与条件は1つだけです)")
        else:
            lines.append(
                f"  **給与条件が {len(only.salary_ids)} つあります。**"
                " どれを使うかは運用者が選んでください (推測しません)。"
            )
    elif matched:
        lines.append(f"  **{len(matched)} 件が一致しました。** 絞れないので選んでください。")
    else:
        lines.append("  **一致しませんでした。** 上の一覧から運用者が選んでください。")
    return "\n".join(lines)
