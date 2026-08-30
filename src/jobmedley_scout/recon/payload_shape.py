"""Read the *shape* of a captured send request. **値は決して出さない。**

段階3の成果物は2つある。送信APIのURLと、**payload の形** である::

    api.send.paid.url_pattern     観測済み (follow-send 2回目)
    api.send.paid.payload_template  ← このモジュールが出す

なぜ形だけなのか。遮断して記録した本文には、送信先の会員IDが載っている。
13.2 は偵察の出力に画面の文言や個人データを残すことを禁じている。そして
**雛形に要るのは値ではなく形である** -- どのキーに何を入れるのかが分かれば、
段階4はその形に自分の値を詰めて送る。

だから ``{"memberId": "3323741"}`` ではなく ``{"memberId": "<string>"}`` を出す。

例外は2つだけある。

1. **自分で書いた目印。** これは値ではなく「ここが本文の入り口だ」という観測
   そのものなので、その位置を名指しする。
2. **運用者自身の求人のID** (:data:`REVEALED_KEYS`)。伏せると、どの求人へ送るのかを
   座標に書けず、送信が永久に組み立たない。``memberId`` と ``searchUuid`` は
   この例外に入れない -- 「IDだから安全」ではなく「**誰の** IDか」で分けている。

キーパスの走査は :mod:`recon.resume_keys` と同じ道具を使う。あちらはレジュメの
キーを値抜きで出すために書かれた (6.4 の取り違え対策) が、**「値を出さずに形
だけ出す」という問題は同じ** なので新しく書き起こさない。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jobmedley_scout.recon.resume_keys import KeyPath, discover_key_paths

#: 値を伏せたときに置く印。
UNKNOWN_VALUE = "<{kind}>"

#: 目印が載っていたキーに置く印。**ここが本文の入り口である。**
#:
#: 語彙は :mod:`api.payloads` の ``PLACEHOLDER_BODY`` と **同じもの** を使う。
#: 別の記法にすると、偵察が出した雛形を座標へ貼っても本文が差し替わらず、
#: ``<本文>`` という文字列がそのままスカウトとして実在の候補者へ飛ぶ。
#: 送信は取り消せない (13.6)。**出力と入力の語彙は揃えておく。**
BODY_MARKER = "{{BODY}}"

#: ``variables`` 以外の最上位キーは **そのまま残す**。
#:
#: 最初の実装はここを落としていた。「問い合わせ文は長いし、雛形に要るのは変数の
#: 形である」という理屈だったが、**GraphQL は ``query`` が無いリクエストを受け
#: 付けない** (persisted query を使う場合は ``extensions`` が代わりに要る)。
#: 落とした雛形は、貼っても送れない雛形である。
#:
#: 問い合わせ文は媒体のAPIの定義そのもので、画面の文言でも個人データでもない
#: ので 13.2 には触れない。伏せるべきは ``variables`` の **値** だけである。

#: 名前だけを出してよいヘッダ。**値は1つも出さない** -- Cookie や
#: Authorization の値はセッションそのものであり、ログに残せば漏洩である
#: (12.7 は資格情報を状態から分離することを求めている)。
#:
#: 名前だけなら構造である。``api.idempotency_header`` は「そういう名前の
#: ヘッダが在るか」だけで決まるので、名前が分かれば足りる。
HEADER_NAMES_ONLY = True


@dataclass(frozen=True)
class PayloadShape:
    """The shape of one captured request. **値を持たない。**"""

    #: GraphQL の操作名。URL の末尾にも出ているので、これ自体は新しい情報ではない。
    operation: str
    #: ``variables`` のキーパスと値の種別 (値は含まれない)。
    keys: tuple[KeyPath, ...]
    #: 目印が載っていたキーパス。空文字なら **本文の入り口が特定できていない**。
    body_key: str
    #: 送ったヘッダの **名前だけ**。値は1つも持たない。
    header_names: tuple[str, ...]
    #: 値を伏せた雛形 (JSON)。``api.send.paid.payload_template`` に転記する。
    template: str

    def unfilled_keys(self) -> tuple[str, ...]:
        """Key paths whose value is still a kind marker. **貼る前に埋める必要がある。**

        本文 (:data:`BODY_MARKER`) は数えない -- あれは送信時にコードが差し込む。
        """
        try:
            parsed = json.loads(self.template)
        except ValueError:  # pragma: no cover - template は自前で作っている
            return ()
        found: list[str] = []

        def _walk(node: object, prefix: str) -> None:
            if isinstance(node, str):
                if node.startswith("<") and node.endswith(">"):
                    found.append(prefix)
                return
            if isinstance(node, Mapping):
                for key, value in node.items():
                    _walk(value, f"{prefix}.{key}" if prefix else str(key))
                return
            if isinstance(node, Sequence) and not isinstance(node, str | bytes):
                for index, value in enumerate(node):
                    _walk(value, f"{prefix}[{index}]")

        _walk(parsed.get("variables"), "variables")
        return tuple(found)

    def render(self) -> str:
        lines = ["送信リクエストの形 (**値は含まれていません** -- 13.2)", ""]
        lines.append(f"  操作名: {self.operation or '(GraphQL ではありません)'}")
        if self.body_key:
            lines.append(f"  本文の入り口: {self.body_key}")
        else:
            # **見つからないことを、見つかったことにしない** (原則3)。
            lines.append(
                "  本文の入り口: **特定できていません** "
                "(目印を運んでいたのに、どのキーに載っていたか辿れませんでした)"
            )
        if self.keys:
            lines.append("  変数のキーパス:")
            lines.extend(f"    {path.render()}" for path in self.keys)
        if unfilled := self.unfilled_keys():
            # **貼っただけでは送れない、と先に言う。** 種別の印が残ったまま送ると、
            # ``<string>`` という文字列がそのまま媒体へ飛ぶ。送信は取り消せない
            # (13.6) ので、埋める必要がある欄を名指ししておく。
            lines.append("  **まだ値が決まっていない欄** (座標に貼る前に埋めること):")
            lines.extend(f"    {key}" for key in unfilled)
        if self.header_names:
            lines.append(f"  ヘッダ名 (**値は出しません**): {', '.join(self.header_names)}")
        return "\n".join(lines)


def _kind_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "unknown"


#: 値をそのまま出してよい欄。**運用者自身の求人のIDだけ。**
#:
#: この2つは運用者が媒体へ公開している求人票を指すもので、13.2 が守ろうとして
#: いる対象 (候補者の氏名・会員番号・年齢・居住地) ではない。伏せると、どの
#: 求人へ送るのかを座標に書けず、**送信が永久に組み立たない**。
#:
#: **``memberId`` と ``searchUuid`` は入れない。** 前者は候補者そのものを指し、
#: 後者はどの検索から辿り着いたかを指す。どちらも伏せたままにする。
#: 「IDだから安全」ではなく、「**誰の** IDか」で分けている。
REVEALED_KEYS: frozenset[str] = frozenset({"jobOfferId", "jobOfferSalaryId"})


def _blank(value: object, sentinel: str, key: str = "") -> object:
    """Replace every scalar with its kind. **目印だけは位置を名指しする。**

    例外は :data:`REVEALED_KEYS` の2欄だけで、そこは値をそのまま残す
    (運用者自身の求人のIDであり、候補者の情報ではない)。
    """
    if isinstance(value, str) and sentinel and sentinel in value:
        return BODY_MARKER
    if isinstance(value, Mapping):
        return {str(k): _blank(item, sentinel, str(k)) for k, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        # **要素は1つに畳む。** 何件送ったかは形ではなく、その回の都合である。
        return [_blank(value[0], sentinel, key)] if value else []
    if key in REVEALED_KEYS and isinstance(value, str | int | float):
        return value
    return UNKNOWN_VALUE.format(kind=_kind_name(value))


def _find_sentinel_key(node: object, sentinel: str, prefix: str = "") -> str:
    """The key path whose value carries the sentinel. ``""`` if none. **Pure.**"""
    if isinstance(node, str):
        return prefix if sentinel and sentinel in node else ""
    if isinstance(node, Mapping):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if found := _find_sentinel_key(value, sentinel, path):
                return found
        return ""
    if isinstance(node, Sequence) and not isinstance(node, str | bytes):
        for index, value in enumerate(node):
            if found := _find_sentinel_key(value, sentinel, f"{prefix}[{index}]"):
                return found
    return ""


def shape_of(
    body: str | None, headers: Mapping[str, str] | None, sentinel: str
) -> PayloadShape | None:
    """The shape of a captured request body. ``None`` if it cannot be read.

    **読めなければ ``None`` を返す。** 「たぶん GraphQL だろう」と形を作れば、
    それは推測で座標を埋めることになる (原則3)。読めなかったことは、読めなかった
    と報告すればよい。
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None

    operation = str(payload.get("operationName") or "")
    variables: Any = payload.get("variables")
    keys = discover_key_paths(variables) if isinstance(variables, Mapping) else ()
    body_key = _find_sentinel_key(variables, sentinel, "variables")
    # **封筒はそのまま、中身の値だけを伏せる。** ``query`` や ``extensions`` を
    # 落とすと、貼っても送れない雛形になる (上の BODY_MARKER の注記を参照)。
    envelope = {str(key): value for key, value in payload.items() if key != "variables"}
    template = json.dumps(
        {**envelope, "variables": _blank(variables, sentinel)},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return PayloadShape(
        operation=operation,
        keys=keys,
        body_key=body_key,
        header_names=_header_names(headers),
        template=template,
    )


def _header_names(headers: Mapping[str, str] | None) -> tuple[str, ...]:
    """Header **names**, sorted. Values are never returned."""
    return tuple(sorted(str(name) for name in (headers or {})))


def idempotency_candidates(header_names: Iterable[str]) -> tuple[str, ...]:
    """Header names that look like an idempotency key. **名前だけで判断する。**

    見つからなければ空。空は「無い」ではなく「**この1回の送信には載っていな
    かった**」である -- 座標 ``api.idempotency_header`` を null と書いてよいかは、
    これだけでは決まらない (9.2 は受け口が無ければ送信済み照会で代替せよと言う)。
    """
    hints = ("idempotenc", "request-id", "requestid", "x-request", "nonce")
    return tuple(sorted(name for name in header_names if any(h in name.lower() for h in hints)))
