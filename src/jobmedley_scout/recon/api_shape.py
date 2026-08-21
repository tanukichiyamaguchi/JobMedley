"""Read the *shape* of the platform's read APIs. **値は決して出さない。**

段階3で送信路は取れた。残っているのは **読み取り側** である::

    api.candidate_list.url_pattern   一覧の取得
    api.resume.url_pattern           レジュメの取得
    resume.fields.*                  レジュメの項目名

そして送信のために、もう1つ急ぐものがある。観測した送信 payload には
``searchUuid`` が載っていた -- 送信は「どの検索から辿り着いた候補者か」に
紐づいている。**その値の出所が一覧の応答にあるはずだが、まだ見ていない。**

**なぜ形だけなのか。** 一覧の応答には氏名・会員番号・年齢・居住地・経歴が入って
いる。13.2 は偵察の出力に個人データを残すことを禁じている。そして座標に要るのは
値ではなく **どのキーに何が入っているか** である。

道具は :mod:`recon.resume_keys` のものをそのまま使う。あちらはレジュメのキーを
値抜きで出すために書かれた (6.4 の取り違え対策) が、**「値を出さずに形だけ出す」
という問題は同じ** なので新しく書き起こさない。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from jobmedley_scout.recon.resume_keys import KeyPath, discover_key_paths

#: 応答を辿る深さ。
#:
#: レジュメの既定 (4) では足りない。GraphQL の応答は
#: ``data.<operation>.edges[].node.<entity>.<field>`` のように包みが深く、
#: 4段だと **中身に届く前に切れる**。切れた先は報告に出ないので、
#: 「その項目は無い」と読み違える (原則2 の静かなゼロ件)。
RESPONSE_DEPTH = 7

#: 報告してよいキーの数の上限。**多すぎる = キーが値になっている兆候** である。
#:
#: 応答が ``{"3323741": {...}, "2973815": {...}}`` のように **会員IDをキーにした
#: 地図** だった場合、キー名そのものが個人データになる。下の
#: :func:`looks_like_an_identifier` がそれを落とすが、落とし損ねた場合の保険として
#: 総数にも上限を置く。
MAX_KEYS_REPORTED = 200

#: 「これはキーの名前ではなく値だ」と判断する形。
_BARE_NUMBER = re.compile(r"^\d+$")
_UUID_LIKE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_LONG_HEX = re.compile(r"^[0-9a-f]{16,}$", re.I)

#: 検索の識別子らしいキー名の断片。**名前で探す。値は見ない。**
#:
#: 送信 payload の ``searchUuid`` に入る値の出所を、一覧の応答の中から
#: **キー名だけで** 当たりを付けるためのもの。当たったからといって座標に書いて
#: よいわけではない -- 名前が似ていることは、同じ値であることの証明ではない
#: (原則3)。報告に「候補」として出すところまでが、この関数の仕事である。
SEARCH_ID_HINTS: tuple[str, ...] = ("searchuuid", "search_uuid", "searchid", "search_id")

#: スカウト送信に要る値の名前。応答のどこかに在るはずのもの。
SEND_INPUT_HINTS: tuple[str, ...] = (
    "searchuuid",
    "memberid",
    "joboffer",
    "jobofferid",
    "joboffersalaryid",
)


def looks_like_an_identifier(key: str) -> bool:
    """Whether this key *name* is really a value in disguise. **Pure.**

    キーの名前は普通スキーマの語なので出してよい。だが応答が「IDをキーにした
    地図」だった場合、キー名は会員番号そのものになる。**それは個人データである。**

    数字だけ・UUID・長い16進は名前ではなく値とみなして落とす。
    """
    return bool(_BARE_NUMBER.match(key) or _UUID_LIKE.match(key) or _LONG_HEX.match(key))


def _safe_path(path: str) -> bool:
    """Whether every segment of a key path is a name rather than a value. **Pure.**"""
    for segment in re.split(r"[.\[\]]+", path):
        if segment and looks_like_an_identifier(segment):
            return False
    return True


@dataclass(frozen=True)
class ObservedCall:
    """One GraphQL read that the platform made. **本文は持たない。**"""

    #: GraphQL の操作名。``""`` なら読めなかった。
    operation: str
    #: 会員IDやクエリ値を伏せたURL。
    redacted_url: str
    method: str
    #: 応答のキーパスと値の種別。**値は含まれない。**
    keys: tuple[KeyPath, ...] = ()
    #: 応答が読めなかった理由 (定型句のみ)。空なら読めた。
    unread_reason: str = ""
    #: 個人データに見えたので落としたキーの数。**落とした事実も観測である。**
    dropped_keys: int = 0

    def search_id_candidates(self) -> tuple[str, ...]:
        """Key paths whose **name** suggests the search identifier. **Pure.**"""
        return _matching(self.keys, SEARCH_ID_HINTS)

    def send_input_candidates(self) -> tuple[str, ...]:
        """Key paths whose **name** matches something the send payload needs."""
        return _matching(self.keys, SEND_INPUT_HINTS)

    def render(self) -> str:
        lines = [f"  操作: {self.operation or '(名前を読めませんでした)'}"]
        lines.append(f"    {self.method} {self.redacted_url}")
        if self.unread_reason:
            # **読めなかったことを、キーが無かったことにしない** (原則2)。
            lines.append(f"    応答を読めませんでした: {self.unread_reason}")
            return "\n".join(lines)
        if not self.keys:
            lines.append("    応答は読めましたが、キーが1つもありませんでした。")
            return "\n".join(lines)
        lines.append(f"    キー ({len(self.keys)} 個。**値は含まれていません** -- 13.2):")
        lines.extend(f"      {path.render()}" for path in self.keys)
        if self.dropped_keys:
            lines.append(
                f"    個人データに見えたので落としたキー: {self.dropped_keys} 個 "
                f"(応答が会員IDをキーにした地図になっている可能性)"
            )
        if found := self.search_id_candidates():
            lines.append(f"    **検索の識別子らしいキー**: {', '.join(found)}")
        return "\n".join(lines)


def _matching(paths: Sequence[KeyPath], hints: Iterable[str]) -> tuple[str, ...]:
    lowered = tuple(hint.lower() for hint in hints)
    found: list[str] = []
    for path in paths:
        last = re.split(r"[.\[\]]+", path.path)[-1].lower().replace("_", "")
        if any(hint.replace("_", "") in last for hint in lowered):
            found.append(path.path)
    return tuple(found)


def operation_name(request_body: str | None) -> str:
    """The GraphQL operation name from a request body. ``""`` if unreadable. **Pure.**"""
    if not request_body:
        return ""
    try:
        payload = json.loads(request_body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("operationName") or "")


def describe_response(body: str | None) -> tuple[tuple[KeyPath, ...], str, int]:
    """Key paths of a response body. Returns ``(keys, unread_reason, dropped)``.

    **値は1つも返らない。** ``KeyPath`` が持つのは経路と値の *種別* だけである
    (:mod:`recon.resume_keys` を参照)。

    キーの名前が値に見えるもの (会員IDをキーにした地図など) は落とし、
    **落とした数だけを報告する** -- 落とした事実は観測であり、隠すと
    「その応答は薄かった」と読み違える。
    """
    if not body:
        return (), "本文が空でした", 0
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        # 生のメッセージは出さない (本文が混ざる)。分類だけを返す。
        return (), "JSONとして読めませんでした", 0
    everything = discover_key_paths(parsed, max_depth=RESPONSE_DEPTH)
    kept = tuple(path for path in everything if _safe_path(path.path))
    dropped = len(everything) - len(kept)
    if len(kept) > MAX_KEYS_REPORTED:
        # **多すぎるものは切る。** 切ったことは数で分かるようにする。
        dropped += len(kept) - MAX_KEYS_REPORTED
        kept = kept[:MAX_KEYS_REPORTED]
    return kept, "", dropped
