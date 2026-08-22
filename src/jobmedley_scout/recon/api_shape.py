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
from urllib.parse import urlsplit

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

#: UUID の形をした文字列。**数えるためだけに使う。値は取り出さない。**
#:
#: 一覧が JSON で来ない場合 (サーバ側で組み立てたHTMLだった場合)、``searchUuid``
#: の値は文書のどこかに埋まっている。キーパスでは辿れないが、**「その形のものが
#: 何個あるか」は数えられる**。0個なら文書には無い、1個以上なら在る --
#: どちらも次にどこを見るかを決める材料になる。
#:
#: **数は個人データではない。** 値そのものは1文字も取り出さない。
_UUID_TOKEN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)

#: 送信 payload が要求するキーの名前。文書の中にこの **名前** が現れるかを数える。
#: 名前はAPIの語彙であって個人データではない。
_SEND_KEY_NAMES: tuple[str, ...] = ("searchuuid", "joboffersalaryid", "jobofferid", "memberid")


def scan_text(body: str | None) -> tuple[int, int]:
    """Count UUID-shaped tokens and send-key *names* in a document. **Pure.**

    返すのは ``(UUIDの形の数, 送信キーの名前の出現数)`` の2つの **数** だけ。
    値も文言も返さない (13.2)。

    JSON として読めない応答 (サーバ側で組み立てたHTML等) に対して、
    「送信に要る値がこの文書に入っているのか」を値を見ずに問うための道具である。
    """
    if not body:
        return 0, 0
    lowered = body.lower()
    return (
        len(set(_UUID_TOKEN.findall(body))),
        sum(lowered.count(name) for name in _SEND_KEY_NAMES),
    )


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
    #: 応答の種別 (``application/json`` 等)。読めなければ空。
    content_type: str = ""
    #: JSON として読めなかった文書の中に在った、UUIDの形の文字列の **数**。
    uuid_like: int = 0
    #: 同じ文書の中に現れた、送信キーの **名前** の出現数。
    send_key_mentions: int = 0
    #: **要求** 本文のキーパスと値の種別。**値は含まれない。** GETなら空。
    #:
    #: 応答の形が分かっても、**要求の形が分からなければ自分では呼べない。**
    #: 実測23回目で候補者一覧のURL (``members/search``) は決まったが、その回の
    #: 報告は応答しか出していなかったので、「何を送れば同じ並びが返るのか」は
    #: 分からないままだった -- 座標が1つ埋まっただけで、呼べるようにはならない。
    request_keys: tuple[KeyPath, ...] = ()
    #: 要求本文を読めなかった理由 (定型句のみ)。空なら読めた、または本文が無い。
    request_unread_reason: str = ""
    #: 要求本文で個人データに見えたので落としたキーの数。
    request_dropped_keys: int = 0

    def search_id_candidates(self) -> tuple[str, ...]:
        """Key paths whose **name** suggests the search identifier. **Pure.**"""
        return _matching(self.keys, SEARCH_ID_HINTS)

    def send_input_candidates(self) -> tuple[str, ...]:
        """Key paths whose **name** matches something the send payload needs."""
        return _matching(self.keys, SEND_INPUT_HINTS)

    def request_lines(self) -> tuple[str, ...]:
        """The shape of what the platform **sent**. 呼ぶために要る。

        **応答と同じ扱いで値は1つも出さない** (13.2)。一覧を要求する本文には
        検索条件 (都道府県・年齢・資格) が載り、そこから個人が絞り込まれうるが、
        出すのはキーの名前と値の種別だけである。
        """
        if self.method in ("GET", "HEAD"):
            return ()
        if self.request_unread_reason:
            return (f"    要求本文: 読めませんでした ({self.request_unread_reason})",)
        if not self.request_keys:
            return ("    要求本文: ありません (本文なしのPOST)",)
        out = [
            f"    **要求本文** のキー ({len(self.request_keys)} 個。"
            f"**値は含まれていません** -- 13.2):"
        ]
        out.extend(f"      {path.render()}" for path in self.request_keys)
        if self.request_dropped_keys:
            out.append(f"    要求本文で落としたキー: {self.request_dropped_keys} 個")
        return tuple(out)

    def render(self) -> str:
        lines = [f"  操作: {self.operation or '(名前を読めませんでした)'}"]
        lines.append(f"    {self.method} {self.redacted_url}")
        if self.content_type:
            lines.append(f"    種別: {self.content_type}")
        # **要求を先に出す。** 応答は長い。呼ぶために要るのは要求の形なので、
        # 何百行のキー一覧の下に埋めない。
        lines.extend(self.request_lines())
        if self.unread_reason:
            # **読めなかったことを、キーが無かったことにしない** (原則2)。
            lines.append(f"    キーパスは取れませんでした: {self.unread_reason}")
            if self.uuid_like or self.send_key_mentions:
                # **値は出さない。数だけ出す。** それでも次に見る先は決まる。
                lines.append(
                    f"    ただし文書の中に UUIDの形が {self.uuid_like} 個、"
                    f"送信キーの名前が {self.send_key_mentions} 回ありました"
                )
                lines.append("    (**値は取り出していません。数だけです** -- 13.2)")
            else:
                lines.append("    文書の中に UUIDの形も送信キーの名前もありませんでした。")
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
        if inputs := self.send_input_candidates():
            # **候補であって決定ではない** (原則3)。名前が似ていることは、
            # 送信payloadが要る値と同じであることの証明ではない。
            shown = ", ".join(inputs[:_MAX_INPUT_CANDIDATES])
            more = len(inputs) - _MAX_INPUT_CANDIDATES
            lines.append(
                f"    送信payloadが要る値らしいキー (候補): {shown}"
                + (f" ほか {more} 個" if more > 0 else "")
            )
        return "\n".join(lines)


#: 送信payloadの候補として並べるキーの上限。全部並べると本命が埋まる。
_MAX_INPUT_CANDIDATES = 8


def _matching(paths: Sequence[KeyPath], hints: Iterable[str]) -> tuple[str, ...]:
    lowered = tuple(hint.lower() for hint in hints)
    found: list[str] = []
    for path in paths:
        last = re.split(r"[.\[\]]+", path.path)[-1].lower().replace("_", "")
        if any(hint.replace("_", "") in last for hint in lowered):
            found.append(path.path)
    return tuple(found)


#: 名前付けから落とすURLの節。**入れ物の名前であってAPIの語彙ではない。**
#: これを残すと どの呼び出しも "api/customers" で始まり、見分けが付かなくなる。
_UNINFORMATIVE_SEGMENTS: frozenset[str] = frozenset({"api", "customers", "graphql"})

#: :func:`recon.open_structure.redact_url` が伏せ字へ置き換えた節。
#: **名前に混ぜない** -- 伏せ字そのものは何も述べていない。
_REDACTED_SEGMENTS: frozenset[str] = frozenset({"{id}", "{value}"})


def operation_name(request_body: str | None, url: str = "") -> str:
    """A readable name for one call. ``""`` when nothing is readable. **Pure.**

    GraphQL なら封筒の ``operationName`` をそのまま使う。

    **RESTには操作名が無い。** 実測23回目の報告は、聴いた19本が **すべて**
    「(名前を読めませんでした)」で始まっていた -- この媒体の読み取りは REST の
    POST なので、GraphQL の封筒を探しても名前は出てこない。名前の無い19本を
    長いURLだけで見分けさせるのは読む側の負担であり、**読めない報告は
    観測していないことに近づく** (実測22回目に同じ失敗をしている)。

    そこでURLの経路から名付ける。伏せ字の節と入れ物の節を落とすので
    ``/api/customers/members/search/`` は ``members/search`` になる。

    **個人データは入らない。** 落とす対象に伏せ字が含まれており、伏せ損ねた
    識別子も :func:`looks_like_an_identifier` が落とす。
    """
    if name := _graphql_operation_name(request_body):
        return name
    return _path_label(url)


def _graphql_operation_name(request_body: str | None) -> str:
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


def _path_label(url: str) -> str:
    """The last two informative path segments of a URL. **Pure.**"""
    if not url:
        return ""
    segments = [
        segment
        for segment in urlsplit(url).path.split("/")
        if segment
        and segment not in _UNINFORMATIVE_SEGMENTS
        and segment not in _REDACTED_SEGMENTS
        and not looks_like_an_identifier(segment)
    ]
    return "/".join(segments[-2:])


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
