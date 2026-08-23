"""``HttpTransport`` の本番実装。**認証済みブラウザの通信路をそのまま使う。**

原則1 が求めているのはこれである -- DOM を操作するのではなく、**運用者が
ログイン済みのブラウザから内部APIを呼ぶ**。Playwright の ``context.request`` は
そのコンテキストの Cookie をそのまま載せるので、セッションを別経路で複製する
必要が無い (複製した瞬間、12.7 が分けたはずの資格情報が増える)。

**ブラウザ依存部はここに閉じ込める** (13.4)。:mod:`api` パッケージは
:class:`~api.transport.HttpTransport` の Protocol だけを知っていればよく、
契約テストはブラウザもネットワークも起動せずに走る。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobmedley_scout.api.transport import HttpResponse

#: 応答本文の取り込み上限。**超えた分は捨てるのではなく、捨てたことを述べる。**
#:
#: 一覧は25件で数十KB程度だが、上限を置かないと想定外の応答でメモリを食う。
#: 切ったことが分かるように印を残す -- 黙って切ると「そこで終わっていた」と
#: 読み違える (原則2)。
MAX_BODY_CHARS = 4_000_000

#: 切り詰めたときに末尾へ付ける印。
TRUNCATION_MARK = "\n<<TRUNCATED>>"


class PlaywrightTransport:
    """Issues requests through an authenticated Playwright browser context.

    **``context.request`` を使う。** ``page.request`` ではないのは、ページの
    ライフサイクル (遷移・再読み込み) に巻き込まれないためである。

    **遮断 (``recon.gate``) はここを通らない。** ルート傍受はページの通信に
    掛かるもので、``context.request`` には掛からない。つまり **このクラスが
    出す通信は止まらない** -- 何を送るかは呼び出し側の責任である。
    送信の門は :func:`api.payloads.assert_fully_filled` と
    :func:`api.payloads.assert_sendable_graphql`、そして 12.5 の
    「送信APIは自動再試行しない」で守る。
    """

    def __init__(self, context: Any) -> None:
        self._context = context

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object] | None = None,
    ) -> HttpResponse:
        """Issue one request and return its status, body and headers.

        **例外を握り潰さない。** 通信できなかったことを「空の応答」として返すと、
        上流はそれを「0件だった」と読む -- 原則2 の静かなゼロ件そのものである。
        """
        response = self._context.request.fetch(
            url,
            method=method,
            headers=dict(headers),
            data=None if json is None else dict(json),
        )
        return HttpResponse(
            status=response.status,
            body_text=_bounded(response.text()),
            headers=_plain(response.headers),
        )


def _bounded(body: str) -> str:
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[:MAX_BODY_CHARS] + TRUNCATION_MARK


def _plain(headers: object) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        return {}
    return {str(name): str(value) for name, value in headers.items()}


__all__ = ["MAX_BODY_CHARS", "TRUNCATION_MARK", "PlaywrightTransport"]
