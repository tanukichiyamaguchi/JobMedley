"""Text normalization shared by every path that compares strings.

8.6 の要点: モデルが返す識別子は大文字小文字や空白が揺れる。**生成側と参照側の
全経路で同じ正規化関数を通すこと。** 片方だけだと静かに不一致する。同じ理由で、
件名照合 (10.2)・自己除外の氏名一致 (8.4)・共通点マッチング (8.3) は、それぞれ
別の関数を持つのではなく、ここの関数を共有する。
"""

from __future__ import annotations

import re
import unicodedata

_WS_RUN = re.compile(r"\s+")
_ALL_WS = re.compile(r"\s")

# 受信箱の行は "Re: <送信した件名>" になる (10.2)。返信が転送・再返信を経ると
# "Re: Re: ..." のように積み重なるため、先頭の返信接頭辞は繰り返し剥がす。
# 全角コロンと日本語の「返信」形も実際に観測されうるので含めてある。
_REPLY_PREFIX = re.compile(
    r"^\s*(?:re|ｒｅ|返信|回答)\s*[:：]\s*",
    re.IGNORECASE,
)


def fold_width(text: str) -> str:
    """NFKC-normalize: full-width ASCII/digits fold to half-width, etc.

    表記のみの差であって実体の差ではない変換だけをここに置くこと。
    """
    return unicodedata.normalize("NFKC", text)


def normalize_ws(text: str) -> str:
    """Collapse internal whitespace runs to one space and strip the ends."""
    return _WS_RUN.sub(" ", text).strip()


def strip_all_ws(text: str) -> str:
    """Remove every whitespace character.

    件名照合はこちらを使う (10.2 の「正規化(空白除去)」)。媒体側が件名を
    折り返したり全角スペースを混ぜたりしても一致するようにするため、
    畳むのではなく除去する。
    """
    return _ALL_WS.sub("", text)


def normalize_identifier(value: str) -> str:
    """Canonical form for identifiers that cross the LLM boundary.

    LLM に識別子を返させる場合、生成側と参照側の **両方** がこの関数を通ること。
    片方だけだと静かに不一致する (8.6)。
    """
    return fold_width(value).strip().casefold()


def normalize_name(value: str) -> str:
    """Canonical form for person names.

    自己除外 (8.4) の氏名一致と、受信箱の返信者名の手掛かりに使う。
    姓名間の空白は媒体・経路によって有無が揺れるため除去する。
    """
    return strip_all_ws(fold_width(value)).casefold()


def strip_reply_prefix(subject: str) -> str:
    """Remove leading ``Re:`` / ``返信:`` markers, however many are stacked."""
    previous = None
    current = subject
    while previous != current:
        previous = current
        current = _REPLY_PREFIX.sub("", current, count=1)
    return current


def normalize_subject(subject: str) -> str:
    """Canonical form for subject matching.

    返信検知の突合キー (10.2)。件名は候補者ごとに個別生成される一点物なので、
    突合キーとして強く機能する -- ただしそれは、送信時と受信時で **同じ**
    正規化を通している場合に限る。
    """
    return strip_all_ws(fold_width(strip_reply_prefix(subject)))
