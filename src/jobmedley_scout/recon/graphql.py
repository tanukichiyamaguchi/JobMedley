"""GraphQL のリクエストが **読み取りだけか** を判定する。純粋。

**なぜこれが要るのか (実測5回目)。**

媒体は GraphQL の単一ページアプリだった。候補者カードのボタンを押すと

    POST /api/customers/graphql/MemberOnScoutProfileModalOfDesktop

が飛ぶ。**画面を開くための読み取りが POST で来る。** 遮断は fail-closed で
「GET/HEAD 以外は全部止める」ので、これも止まる。止めるとモーダルは中身を
得られず、媒体の共通エラー処理が働いて ``/customers/network_error/`` へ
飛ばされ、探索はそこで終わる。

つまり **「非GETを全部止める」ままでは、段階3は原理的に終われない。**
送信画面へ辿り着くには読み取りを通す必要があり、読み取りは POST で来る。

**そこで、通す条件を「メソッド」から「操作の種類」へ移す。** GraphQL では
読み取りは ``query``、状態変更は ``mutation`` であると **仕様で決まっている**。
スカウト送信は状態変更なので ``mutation`` である。だから

* ``query`` だけの文書 → 通す (画面が開く)
* ``mutation`` を含む / 判定できない → 止めて記録する (送信はここに入る)

これは fail-closed を弱めるが、弱め方を **仕様上の区別1点に限定** している。
「安全そうだから通す」ではない。判定できないものは全て止める側に倒す --
本文が読めない、JSONでない、``query`` フィールドが無い、URLが GraphQL の
エンドポイントに見えない、いずれも「通さない」である。

**残るリスクを隠さない。** 媒体が送信を ``query`` として実装していれば、この
判定は通してしまう。GraphQL のクライアント (Apollo 等) もサーバも通常そうは
作らないが、可能性はゼロではない。この緩和を使うのは探索コマンド
(``capture-open``) だけで、送信路が判明した後の ``capture-send`` は
「非GETは全部止める」ままにしてある。
"""

from __future__ import annotations

import json
import re
from typing import Any

#: URL が GraphQL のエンドポイントを指していること。**独立した2つ目の条件。**
#: これが無いと、``query`` というフィールドを持つ JSON を受け取る普通の REST API
#: まで読み取り扱いになる。
_GRAPHQL_IN_URL = re.compile(r"graphql", re.IGNORECASE)

#: 文書中の操作種別。``query`` 以外が1つでもあれば読み取りではない。
_OPERATION = re.compile(r"\b(query|mutation|subscription)\b")

#: 文字列リテラルとコメント。ここに現れる ``mutation`` は操作ではないので、
#: 判定前に取り除く (取り除かなくても「止める側」に倒れるだけで危険は無い)。
_STRING_LITERAL = re.compile(r'"""(?:.|\n)*?"""|"(?:[^"\\]|\\.)*"')
_COMMENT = re.compile(r"#[^\n]*")


def _document_is_query_only(document: str) -> bool:
    """Whether a GraphQL document contains only ``query`` operations. **Pure.**"""
    stripped = _COMMENT.sub(" ", _STRING_LITERAL.sub('""', document))
    kinds = {match.group(1) for match in _OPERATION.finditer(stripped)}
    if kinds - {"query"}:
        return False  # mutation / subscription が混ざっている
    if kinds:
        return True
    # 操作キーワードが1つも無い = 無名操作。仕様上は query だが、**波括弧で
    # 始まっていること** を確かめる。確かめられない形は通さない。
    return stripped.lstrip().startswith("{")


def _payload_is_query_only(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    document = payload.get("query")
    if not isinstance(document, str) or not document.strip():
        return False
    return _document_is_query_only(document)


def is_read_only_graphql(url: str, body: str | None) -> bool:
    """Whether this request is a GraphQL read and nothing else. **Pure.**

    **判定できないものは全て False** (通さない)。ここで「たぶん読み取り」を
    True にすると、その一件が取り消せない送信になりうる。
    """
    if not body or not _GRAPHQL_IN_URL.search(url):
        return False
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    if isinstance(payload, list):
        # まとめ送り (batched)。**1つでも読み取りでなければ全体を止める。**
        return bool(payload) and all(_payload_is_query_only(item) for item in payload)
    return _payload_is_query_only(payload)
