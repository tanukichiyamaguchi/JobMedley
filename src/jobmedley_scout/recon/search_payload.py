"""一覧の要求本文を、**貼れる雛形**にする。純粋。

実測35回目 (preview 1回目) で HTTP 500 が返った。原因は座標に入っていた雛形が
偵察の印のままだったことである::

    {"age": {"from": "<string>", "to": "<string>"},
     "favorite": "<bool>", "desired_features": ["<number>"], ...}

40キーのうち差し込んでいたのは3つだけで、残りは ``<bool>`` や ``<number>`` と
いう **文字列** のまま飛んでいた。媒体が500を返すのは当然である。

**この本文は運用者自身の検索条件である。** 年齢の範囲、希望エリア、職種、
各種の絞り込みフラグ -- 保存した検索条件そのもので、**候補者の情報ではない**。
返ってくる結果が候補者であって、送る質問は運用者の設定である。だから値を出す
(:data:`recon.payload_shape.REVEALED_KEYS` と同じ考え方 -- 「IDだから安全」では
なく「**誰の** ものか」で分ける)。

**1つだけ条件付きで伏せる。** ``member_id`` は「この会員だけを対象にする」と
いう絞り込みで、埋まっていれば候補者を名指しする。空なら誰も名指ししていない
ので、そのまま出す -- 空を伏せると貼った雛形が使えなくなり、運用者が
「たぶん空だろう」と **推測で書く** ことになる (原則3)。空かどうかは値ではなく
形なので、報告に出しても13.2 に触れない。

なぜ推測で組み立てないか
------------------------

``nav.candidate_list_url`` の問い合わせ文字列には、同じ条件が別の綴りで載って
いる (``da[0][pid]`` / ``df[0]`` / ``gdr[0]`` ...)。そこから本文を組み立てる
ことは **できそうに見える** が、``da`` が ``desired_areas`` で ``df`` が
``desired_features`` だというのは推測である。当たっていても、当たったことを
確かめる手段が無い。だから観測した本文をそのまま使う。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

#: 埋まっていたら値を伏せる欄。**候補者を名指ししうる唯一の絞り込み。**
WITHHELD_KEYS: Final[frozenset[str]] = frozenset({"member_id"})

#: 実行時に差し替える欄と、その差し込み記法。**キーパスで指定する。**
#:
#: 値の一致で探さない -- ``pagination.limit`` が 25 のとき、別の欄の 25 まで
#: 巻き込む。位置で決めれば、たまたま同じ値の欄を壊さない。
#:
#: 記法は :mod:`runtime.commands.ingest` の3つの定数と揃っていること。揃って
#: いないと、貼った雛形のページ番号が差し替わらないまま何度も1ページ目を引き、
#: 報告だけが「2ページ目」と言う (原則2)。
RUNTIME_SLOTS: Final[dict[str, str]] = {
    "customer_search_condition_id": "{{SEARCH_CONDITION_ID}}",
    "pagination.limit": "{{PAGE_SIZE}}",
    "pagination.page": "{{PAGE}}",
}

#: 伏せた欄に置く印。**``<...>`` の形にしてあるので、貼ったまま呼べば門が止める**
#: (:func:`api.payloads.assert_fully_filled`)。伏せたことを運用者に決めさせる。
WITHHELD_MARKER: Final[str] = "<withheld>"

#: 運用者自身の検索条件番号。報告に出して config.yaml と突き合わせてもらう。
CONDITION_KEY: Final[str] = "customer_search_condition_id"

#: 2026-08-22 observe-api 4回目で実測した、絞り込みの最上位キー (40キーのうち)。
#:
#: **消えたら報告する。** 絞り込みの欄が落ちると、別の母集団を引いたまま気付かない
#: (原則2) -- 例外は出ず、返る人が変わるだけである。ここに並べてあるのは
#: 「以前は在った」という事実だけで、**在るべきという要求ではない**。媒体が
#: 画面を作り変えて条件の綴りが変われば、正しく消える。だから観測のたびに
#: 差分を出し、**人が判断する** (原則3)。
OBSERVED_FILTER_KEYS: Final[tuple[str, ...]] = (
    "addresses",
    "age",
    "career_job_categories",
    "career_job_contents",
    "careers",
    "customer_search_condition_id",
    "desired_areas",
    "desired_features",
    "desired_job_category_ids",
    "desired_join_times",
    "employment_statuses",
    "employment_types",
    "favorite",
    "genders",
    "include_qualifications_acquisition_scheduled",
    "last_education_ids",
    "last_login",
    "member_id",
    "member_qualification_ids",
    "nav_type",
    "pagination",
    "qualifications_match_all",
    "read_profile",
    "recently_registered",
    "recommend",
    "scout",
    "self_pr",
    "sort",
)


def is_empty(value: object) -> bool:
    """Whether a captured value names nothing. **``0`` や ``False`` は空ではない。**

    ``not value`` で書くと ``0`` を空と読む。絞り込みの ``0`` は「指定なし」を
    表す値かもしれず、それは空とは違う。**形で判定する。**
    """
    if value is None:
        return True
    if isinstance(value, str | bytes):
        return len(value) == 0
    if isinstance(value, Mapping | list | tuple | set):
        return len(value) == 0
    return False


@dataclass(frozen=True)
class SearchTemplate:
    """The captured body turned into a coordinate value, plus what happened to it."""

    #: 貼れる形の JSON 文字列。
    template: str
    #: 観測した検索条件番号 (運用者自身の値)。取れなければ空文字。
    condition_id: str
    #: 埋まっていたので伏せた欄のキーパス。
    withheld: tuple[str, ...] = ()
    #: 差し込み記法へ置き換えられた欄のキーパス。
    slotted: tuple[str, ...] = ()
    #: 本文に **無かった** 差し込み欄。空でないなら本文の形が変わっている。
    missing_slots: tuple[str, ...] = ()
    #: 前回の観測に在って今回は無かった絞り込みキー。**判断は人がする。**
    vanished_filters: tuple[str, ...] = ()
    #: 前回の観測に無かった絞り込みキー。媒体が条件を増やした可能性。
    new_filters: tuple[str, ...] = ()

    def usable(self) -> bool:
        """Whether this template can be pasted as-is.

        **差し込み欄が1つでも欠けていたら使えない。** 特に ``pagination.page``
        が欠けると、ページ繰りが毎回1ページ目を引きながら報告だけが進む --
        静かな重複であり、静かなゼロ件と同じ病気である (原則2)。
        """
        return not self.missing_slots


def _walk(node: Any, path: str, found: dict[str, list[str]]) -> Any:
    if isinstance(node, Mapping):
        out: dict[str, Any] = {}
        for key, value in node.items():
            name = str(key)
            child = f"{path}.{name}" if path else name
            if name in WITHHELD_KEYS and not is_empty(value):
                found["withheld"].append(child)
                out[name] = WITHHELD_MARKER
            elif child in RUNTIME_SLOTS:
                found["slotted"].append(child)
                out[name] = RUNTIME_SLOTS[child]
            else:
                out[name] = _walk(value, child, found)
        return out
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        return [_walk(item, f"{path}[{index}]", found) for index, item in enumerate(node)]
    return node


def as_template(body: str) -> SearchTemplate | None:
    """The captured request body -> a template ready to paste. ``None`` if unreadable.

    **読めなければ ``None`` を返す。** 「たぶんこういう形だろう」と作れば、それは
    推測で座標を埋めることになる (原則3)。
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, Mapping):
        return None

    found: dict[str, list[str]] = {"withheld": [], "slotted": []}
    walked = _walk(parsed, "", found)
    slotted = tuple(found["slotted"])
    return SearchTemplate(
        template=json.dumps(walked, ensure_ascii=False, indent=2, sort_keys=True),
        condition_id=_condition_id(parsed),
        withheld=tuple(found["withheld"]),
        slotted=slotted,
        missing_slots=tuple(key for key in RUNTIME_SLOTS if key not in slotted),
        vanished_filters=tuple(key for key in OBSERVED_FILTER_KEYS if key not in parsed),
        new_filters=tuple(str(key) for key in parsed if str(key) not in OBSERVED_FILTER_KEYS),
    )


def _condition_id(parsed: Mapping[str, Any]) -> str:
    """The operator's own saved-search number, as text. Empty if absent.

    **出してよい値である。** 誰の情報かで分けると、これは運用者の設定であって
    候補者ではない (13.2 が守るのは候補者の氏名・会員番号・年齢・居住地)。
    """
    value = parsed.get(CONDITION_KEY)
    if value is None or isinstance(value, Mapping | list | tuple | set):
        return ""
    return str(value)


__all__ = [
    "CONDITION_KEY",
    "OBSERVED_FILTER_KEYS",
    "RUNTIME_SLOTS",
    "WITHHELD_KEYS",
    "WITHHELD_MARKER",
    "SearchTemplate",
    "as_template",
    "is_empty",
]
