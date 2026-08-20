"""Read the *shape* of a captured send request. **値は決して出さない。**

段階3の成果物は2つある。送信APIのURLと、**payload の形** である::

    api.send.paid.url_pattern     観測済み (follow-send 2回目)
    api.send.paid.payload_template  ← このモジュールが出す

なぜ形だけなのか。遮断して記録した本文には、送信先の会員IDが載っている。
13.2 は偵察の出力に画面の文言や個人データを残すことを禁じている。そして
**雛形に要るのは値ではなく形である** -- どのキーに何を入れるのかが分かれば、
段階4はその形に自分の値を詰めて送る。

だから ``{"memberId": "3323741"}`` ではなく ``{"memberId": "<string>"}`` を出す。
唯一の例外は **自分で書いた目印** で、これは値ではなく「ここが本文の入り口だ」
という観測そのものなので、その位置を名指しする。

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
BODY_MARKER = "<本文>"

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


def _blank(value: object, sentinel: str) -> object:
    """Replace every scalar with its kind. **目印だけは位置を名指しする。**"""
    if isinstance(value, str) and sentinel and sentinel in value:
        return BODY_MARKER
    if isinstance(value, Mapping):
        return {str(key): _blank(item, sentinel) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        # **要素は1つに畳む。** 何件送ったかは形ではなく、その回の都合である。
        return [_blank(value[0], sentinel)] if value else []
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
    # **``query`` は出さない。** GraphQL の問い合わせ文は長く、雛形に要るのは
    # 変数の形である。操作名はURLにも出ているので、そちらで足りる。
    keys = discover_key_paths(variables) if isinstance(variables, Mapping) else ()
    body_key = _find_sentinel_key(variables, sentinel, "variables")
    template = json.dumps(
        {"operationName": operation, "variables": _blank(variables, sentinel)},
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
