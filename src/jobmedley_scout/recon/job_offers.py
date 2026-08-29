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

**2026-08-29 実測32回目で2つ分かった。**

1. **見出しが読めなかった。** 「80字以内の文字列」で絞ったが、求人票の欄には
   改行入りの短い文が多い (福利厚生・研修・休日の備考)。80字に収まるので通り、
   報告が改行で散らばって読めなくなった。**改行を含む欄は見出しにしない。**
   あわせて、身元を指す欄を名前で優先し、数も絞る。
2. **欲しい求人が入っていなかった。** 返ったのは1件だけで、コールセンターの
   求人だった。歯科衛生士の求人は媒体の画面に在るのに、この応答に無い。
   **「1件しか無い」のか「1件しか返っていない」のかは、この応答だけでは
   決まらない。** だから封筒の目盛り (total / limit / page など) を報告に出す。
   目盛りが読めれば、次に何をすべきかが推測ではなく観測で決まる。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: 求人の並びが入っているキーの名前。**位置は固定しない。**
#:
#: ``data.job_offers[]`` で観測したが、封筒の形が変わっても壊れないよう
#: 名前で探す。見つからなければ「見つからなかった」と報告する (推測しない)。
JOB_OFFERS_KEY: Final[str] = "job_offers"

#: 給与条件の並びが入っているキーの名前。
SALARIES_KEY: Final[str] = "job_offer_salaries"

#: 見出しに使う文字列の長さの上限。これを超える欄は本文 (説明文) とみなして出さない。
MAX_LABEL_CHARS: Final[int] = 80

#: 見出しに使う欄を、**この順で** 探す。実測32回目で観測したキー名である。
#:
#: 名前で優先するのは、身元を指す欄と説明文の欄を長さだけでは分けられないため。
#: ``suggest_name`` が先頭なのは、媒体の検索候補にそのまま出る文字列だからで、
#: 運用者が画面で見ているものと同じ形になる。
PREFERRED_LABEL_KEYS: Final[tuple[str, ...]] = (
    "suggest_name",
    "job_category_name",
    "job_title",
    "facility_name_with_job_category",
    "employment_type",
    "appeal_title",
)

#: 1件あたりに出す見出しの数。**全部出すと読めない** (実測32回目は15欄出た)。
MAX_LABELS: Final[int] = 4

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


def _usable_label(value: object) -> str | None:
    """A short, single-line string, or ``None``.

    **改行を含む欄は見出しにしない。** 実測32回目でここが効かず、報告が改行で
    散らばって読めなくなった -- 求人票には「80字以内だが改行入り」の欄が多い
    (福利厚生・研修・休日の備考)。長さだけでは本文と見出しを分けられない。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_LABEL_CHARS:
        return None
    if "\n" in text or "\r" in text:
        return None
    return text


def _labels(offer: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    """Short direct string fields, for the operator to tell offers apart.

    **入れ子は辿らない。** 辿ると、この endpoint が将来なにを抱えるか分からない
    まま全部を印字することになる。

    身元を指す欄を名前で優先し、足りなければ他の短い1行の欄で補う。全部出すと
    読めない (実測32回目は1件で15欄出た)。
    """
    out: list[tuple[str, str]] = []
    for key in PREFERRED_LABEL_KEYS:
        text = _usable_label(offer.get(key))
        if text is not None:
            out.append((key, text))
    if len(out) < MAX_LABELS:
        taken = {key for key, _ in out}
        for key, value in offer.items():
            if len(out) >= MAX_LABELS:
                break
            if not isinstance(key, str) or key == "id" or key in taken:
                continue
            text = _usable_label(value)
            if text is not None:
                out.append((key, text))
    return tuple(out[:MAX_LABELS])


def envelope_meta(body: object) -> tuple[tuple[str, str], ...]:
    """The envelope's own scalars — ``total`` / ``limit`` / ``page`` and friends.

    **名前を決め打ちしない。** 求人の並びと同じ階層にある数値・真偽値・短い
    文字列を、名前を問わず拾う。目盛りの呼び名は媒体が決めることなので、
    こちらが名前を知っている前提で書くと、名前が違うだけで黙って0件になる。

    **これが要る理由。** 実測32回目、返った求人は1件だけで、欲しい求人は
    入っていなかった。「1件しか無い」のか「1件しか返っていない」のかは、
    求人の並びだけを見ても決まらない。目盛りが読めれば、次にすることが
    推測ではなく観測で決まる (原則2/原則3)。
    """
    holder = _find_offer_holder(body)
    if holder is None:
        return ()
    out: list[tuple[str, str]] = []
    for key, value in holder.items():
        if not isinstance(key, str) or key == JOB_OFFERS_KEY:
            continue
        if isinstance(value, bool | int | float):
            out.append((key, str(value)))
        elif (text := _usable_label(value)) is not None:
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


def _find_offer_holder(node: object, depth: int = MAX_DEPTH) -> Mapping[str, object] | None:
    """Locate the object that **holds** the ``job_offers`` array. **名前で探す。**

    並びそのものではなく持ち主を返すのは、目盛り (total / limit / page) が並びの
    隣に置かれるからである。並びだけを返していると、そこへ手が届かない。
    """
    if depth <= 0:
        return None
    if isinstance(node, Mapping):
        found = node.get(JOB_OFFERS_KEY)
        if isinstance(found, Sequence) and not isinstance(found, str | bytes):
            return node
        for value in node.values():
            deeper = _find_offer_holder(value, depth - 1)
            if deeper is not None:
                return deeper
        return None
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        for item in node:
            deeper = _find_offer_holder(item, depth - 1)
            if deeper is not None:
                return deeper
    return None


def _find_offer_array(node: object, depth: int = MAX_DEPTH) -> Sequence[object] | None:
    """Locate the ``job_offers`` array anywhere in the envelope."""
    holder = _find_offer_holder(node, depth)
    if holder is None:
        return None
    found = holder.get(JOB_OFFERS_KEY)
    return found if isinstance(found, Sequence) and not isinstance(found, str | bytes) else None


#: 取り直すときに要求する件数。**媒体が受け付けるかは分からない。**
#:
#: 実測32回目で返ったのは1件だけだった。画面が要求した ``limit`` をそのまま
#: 大きくして引き直す -- 受け付けなければ同じものが返るだけで、副作用は無い
#: (GET の読み取りである)。
RETRY_LIMIT: Final[int] = 100


def widen_limit(url: str, limit: int = RETRY_LIMIT) -> str:
    """Rewrite the observed URL to ask for more rows. **Pure.**

    **URLは観測したものをそのまま使う。** 経路もパラメータ名もこちらで
    組み立てない -- 組み立てた瞬間、当たったかどうかが分からなくなる (原則3)。
    ここでするのは ``limit`` の値の差し替えだけで、無ければ足す。
    """
    split = urlsplit(url)
    pairs = parse_qsl(split.query, keep_blank_values=True)
    replaced = [(k, str(limit) if k == "limit" else v) for k, v in pairs]
    if not any(k == "limit" for k, _ in replaced):
        replaced.append(("limit", str(limit)))
    return urlunsplit(split._replace(query=urlencode(replaced)))


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


def render_offers(
    offers: Sequence[JobOffer],
    *,
    wanted: str,
    meta: Sequence[tuple[str, str]] = (),
) -> str:
    """The operator-facing report. **0件でも黙らない** (原則2)."""
    lines = ["求人IDの候補 (**運用者自身の公開求人です。候補者の情報ではありません**)", ""]
    if meta:
        # **封筒の目盛りを必ず出す。** 「1件しか無い」のか「1件しか返っていない」
        # のかは、求人の並びだけを見ても決まらない (実測32回目)。
        lines.append("  封筒の目盛り (この応答が全部かどうかの手掛かり):")
        lines.extend(f"    {key} = {value}" for key, value in meta)
        lines.append("")
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
