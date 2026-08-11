"""Noise removal for reply-detection reconnaissance (10.6).

**このモジュールを受信箱の解析より先に書く。** CI上で試行錯誤する場合、1回の実行で
得られる診断情報の質がイテレーション速度をそのまま決める。生のHTMLや応答一覧をそのまま
ログに流すと、次に見るべき場所を探すだけで1往復を消費する。

3つの道具しか置いていない。どれも「1回の実行で次の一手が決まる」ためのものである。

* :func:`structure_digest` -- 構造だけを残す。head/script/style を落とし、
  **非ASCIIの連続を件数マーカーに置換する**。氏名や本文がジョブログに残らない (13.2)。
* :func:`index_responses` -- 応答一覧から **静的資産を除外** してから並べる。
  サイズ順に並べれば「データ応答は大きいはず」で当たると考えたところ、
  先頭に出てきたのはJSライブラリのバンドルだった。除外が先、順位付けは後。
* :func:`fallback_tokens` -- JSON解析に失敗したときでも構造を目視できるように、
  英数字トークンだけを取り出す。**応答がJSONとは限らない。**
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: 1件あたりの要約の既定上限。設定の ``reply.response_capture_char_cap`` を
#: 渡せるように引数にしてある。ジョブログは長さそのものが可読性の敵。
DEFAULT_DIGEST_CHAR_CAP: Final[int] = 2000

#: 非ASCIIの連続を置き換えるマーカー。**マーカー自体はASCIIであること** --
#: マーカーに日本語を使うと、置換したのに結局ログが非ASCIIまみれになる。
NON_ASCII_MARKER: Final[str] = "[#{count}]"

#: 伏せたメールアドレスの跡。件数ではなく存在だけを残す。
EMAIL_MARKER: Final[str] = "[@]"

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# 終了タグが欠けた断片を掴まされることがあるので、末尾までを代替の終端にする。
_HEAD = re.compile(r"<head\b[^>]*>.*?(?:</head>|\Z)", re.IGNORECASE | re.DOTALL)
_SCRIPT = re.compile(r"<script\b[^>]*>.*?(?:</script>|\Z)", re.IGNORECASE | re.DOTALL)
_STYLE = re.compile(r"<style\b[^>]*>.*?(?:</style>|\Z)", re.IGNORECASE | re.DOTALL)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]+")
_WS_RUN = re.compile(r"\s+")
_TOKEN = re.compile(r"[A-Za-z0-9_]+")

#: 拡張子で静的資産と分かるもの。クエリ文字列を落としてから判定する。
STATIC_ASSET_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        ".js",
        ".mjs",
        ".css",
        ".map",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".avif",
        ".ico",
        ".bmp",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp4",
        ".webm",
        ".mp3",
        ".wasm",
    }
)

#: Content-Type で静的資産と分かるもの。拡張子の無いCDN配信を拾うために併用する。
STATIC_ASSET_CONTENT_TYPE_PREFIXES: Final[tuple[str, ...]] = (
    "image/",
    "font/",
    "audio/",
    "video/",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
    "application/font",
    "application/wasm",
)

#: データ応答らしさの判定に使う Content-Type。
DATA_CONTENT_TYPE_HINTS: Final[tuple[str, ...]] = ("json", "text/plain", "xml")


def structure_digest(
    html_or_json: str,
    *,
    max_chars: int = DEFAULT_DIGEST_CHAR_CAP,
) -> str:
    """Reduce a captured body to the part that helps identify its structure.

    Strips comments, ``<head>``, ``<script>`` and ``<style>``, masks e-mail
    addresses, replaces every run of non-ASCII characters with a count marker,
    collapses whitespace and truncates to ``max_chars``.
    """
    # 13.2: 個人情報をジョブログに出さない。日本語の氏名・本文はすべて非ASCIIの
    # 連続として現れるので、連続ごと件数に潰せば構造だけが残る。**件数を残すのは、
    # 「そこに何文字入っていたか」が行の同定と桁数の確認に効くため。**
    stripped = _HTML_COMMENT.sub(" ", html_or_json)
    stripped = _HEAD.sub(" ", stripped)
    stripped = _SCRIPT.sub(" ", stripped)
    stripped = _STYLE.sub(" ", stripped)
    # メールアドレスはASCIIなので非ASCII置換をすり抜ける。先に伏せる (13.2)。
    stripped = _EMAIL.sub(EMAIL_MARKER, stripped)
    stripped = _NON_ASCII_RUN.sub(
        lambda m: NON_ASCII_MARKER.format(count=len(m.group(0))), stripped
    )
    stripped = _WS_RUN.sub(" ", stripped).strip()
    if max_chars > 0 and len(stripped) > max_chars:
        dropped = len(stripped) - max_chars
        return f"{stripped[:max_chars]}...[+{dropped}chars]"
    return stripped


@dataclass(frozen=True)
class ResponseEntry:
    """One response observed while the inbox page loaded."""

    url: str
    content_type: str = ""
    size: int = 0


class ExclusionReason(StrEnum):
    """Why a response was kept out of the ranked candidate list."""

    STATIC_ASSET = "static_asset"


@dataclass(frozen=True)
class ExcludedResponse:
    """A response that was excluded, and why.

    捨てたものも返すのは、除外規則が効きすぎて本命まで落としていた場合に
    それを1回の実行で気付けるようにするため (10.6)。
    """

    entry: ResponseEntry
    reason: ExclusionReason


@dataclass(frozen=True)
class ResponseIndex:
    """The response list, split into plausible data responses and excluded assets."""

    candidates: tuple[ResponseEntry, ...]
    excluded: tuple[ExcludedResponse, ...]

    def best(self) -> ResponseEntry | None:
        """The most likely inbox-list response, or ``None`` if nothing survived."""
        return self.candidates[0] if self.candidates else None


def _url_suffix(url: str) -> str:
    """The lower-cased file suffix of ``url``, ignoring query and fragment."""
    path = url.split("#", 1)[0].split("?", 1)[0]
    last_segment = path.rsplit("/", 1)[-1]
    if "." not in last_segment:
        return ""
    return f".{last_segment.rsplit('.', 1)[-1].lower()}"


def is_static_asset(entry: ResponseEntry) -> bool:
    """Whether ``entry`` is a script/style/image/font rather than data."""
    if _url_suffix(entry.url) in STATIC_ASSET_SUFFIXES:
        return True
    content_type = entry.content_type.split(";", 1)[0].strip().lower()
    return content_type.startswith(STATIC_ASSET_CONTENT_TYPE_PREFIXES)


def _looks_like_data(entry: ResponseEntry) -> bool:
    content_type = entry.content_type.lower()
    return any(hint in content_type for hint in DATA_CONTENT_TYPE_HINTS)


def index_responses(entries: Iterable[ResponseEntry]) -> ResponseIndex:
    """Rank observed responses by how likely each is to carry the inbox list.

    除外が先、順位付けが後。この順序が仕様である (10.6): 「データ応答は大きいはず」
    としてサイズ順に並べた結果、先頭に来たのはJSライブラリのバンドルだった。
    静的資産を落とさないまま並べ替えると、上位が全部ノイズで埋まる。
    """
    candidates: list[ResponseEntry] = []
    excluded: list[ExcludedResponse] = []
    for entry in entries:
        if is_static_asset(entry):
            excluded.append(ExcludedResponse(entry=entry, reason=ExclusionReason.STATIC_ASSET))
        else:
            candidates.append(entry)
    # 並び順: まず Content-Type がデータらしいもの、その中でサイズの大きい順。
    # サイズを **第1キーにしない** のが要点 -- 第1キーにすると上の事故に戻る。
    # URL は同点時の安定化のためだけに入れてある (実行ごとに順序が変わると
    # 「前回と何が違うのか」の比較ができなくなる)。
    candidates.sort(key=lambda entry: (not _looks_like_data(entry), -entry.size, entry.url))
    return ResponseIndex(candidates=tuple(candidates), excluded=tuple(excluded))


def fallback_tokens(
    body: str,
    *,
    min_length: int = 2,
    max_tokens: int = 200,
) -> tuple[str, ...]:
    """Alphanumeric tokens from a body that could not be parsed as JSON.

    応答がJSONとは限らない (10.6)。解析に失敗したときに空の診断しか出ないと、
    次の一手が「もう一度キャプチャする」しかなくなる。キー名らしき断片だけでも
    出しておけば、構造は目視で追える。

    ASCII の英数字だけを拾うので、氏名や本文が混ざらない (13.2)。
    """
    seen: dict[str, None] = {}
    for match in _TOKEN.finditer(body):
        token = match.group(0)
        if len(token) < min_length:
            continue
        seen.setdefault(token, None)
        if len(seen) >= max_tokens:
            break
    return tuple(seen)
