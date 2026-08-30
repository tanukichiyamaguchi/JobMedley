"""CSRFトークンをページから読み、要求ヘッダに載せる。**値はどこにも出さない。**

実測41回目、レジュメAPIが 5万字の HTML を返した。実測42回目に理由が出た --
ブラウザは ``x-csrf-token`` を付けており、こちらは ``Content-Type`` しか付けて
いなかった。トークンの無い POST を弾いてログイン画面へ転送するのは、この種の
フレームワークの標準の挙動である。

実測43回目に出所が出た。ページの285個の名前のうち、当たったのは
``meta[csrf-token]`` の1つだけだった。

**値を持ち回る範囲を最小にしてある。** 読んだトークンは要求ヘッダの辞書に入って
そのまま送られ、報告にも例外文にも入らない。トークンはそれだけで POST を通せる
鍵なので、ログに出れば誰でもこの運用者として書き込める (12.7)。

**残り3つのヘッダは足していない。** ``x-customer-user-id`` /
``x-customer-user-email`` / ``x-experiment-data`` は出所が見つからず、
**要るかどうかも分かっていない**。要ると決めつけて足せば、それは推測である
(原則3)。1つだけ変えて結果を見る -- csrf だけで通れば、残りは不要だったと
**観測で** 分かる。
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from jobmedley_scout.config.placeholders import Coord, is_resolved, require

#: 値が読めたかどうかを報告する言葉。**値そのものは決して出さない。**
FOUND = "取れました (値は出しません)"
NOT_FOUND = "**取れませんでした**"
NOT_REQUIRED = "この媒体では要りません (座標が null)"


def read_csrf_token(page: Any, meta_name: str) -> str | None:
    """The token in ``<meta name="...">``, or ``None``. **例外を漏らさない。**

    例外のメッセージにページの中身が混ざる経路を作らないため、失敗は ``None`` に
    落とす。**「取れなかった」ことは呼び出し側が報告する** -- 黙って進むと、
    ヘッダの無い要求が飛んで HTML が返り、それが「0件」として現れる (原則2)。
    """
    token: object = None
    with suppress(Exception):
        token = page.get_attribute(f'meta[name="{meta_name}"]', "content")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def csrf_headers(
    page: Any,
    header_name: Coord[str | None],
    meta_name: Coord[str | None],
    *,
    used_by: str,
) -> tuple[dict[str, str], str]:
    """Headers to add, and **a report line that never contains the token**.

    座標のどちらかが ``null`` なら「この媒体では要らない」という確定した答えなので、
    何も足さない。未確定 (``UNRESOLVED``) なら :func:`require` が止める --
    知らないまま送ると、失敗が「0件」として現れる。
    """
    if not is_resolved(header_name) or not is_resolved(meta_name):
        # 未確定。**黙って足さないのではなく、確定させてから通す。**
        require(header_name, used_by=used_by)
        require(meta_name, used_by=used_by)
    name = require(header_name, used_by=used_by)
    meta = require(meta_name, used_by=used_by)
    if name is None or meta is None:
        return {}, NOT_REQUIRED
    token = read_csrf_token(page, meta)
    if token is None:
        return {}, NOT_FOUND
    return {name: token}, FOUND


__all__ = ["FOUND", "NOT_FOUND", "NOT_REQUIRED", "csrf_headers", "read_csrf_token"]
