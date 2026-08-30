"""HTTP transport abstraction.

13.4: 「API層はHTTPクライアントを差し替え可能にし、URL・ヘッダ・リクエストボディの
契約を **ネットワークなしで固定するテスト** を書く。**送信APIは契約テストが唯一の
防波堤です。**」

本番の実装 (Playwright の ``context.request``) は :mod:`browser.transport` に置いて
あり、本モジュールはブラウザに一切依存しない。これにより ``api`` パッケージ全体が
ブラウザ無しで import でき、契約テストがネットワークもブラウザも起動せずに走る。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class HttpResponse:
    """A transport-agnostic HTTP response."""

    status: int
    body_text: str
    headers: Mapping[str, str] = field(default_factory=dict)
    #: **実際に応答を返したURL。** 頼んだ先と違うなら、転送されている。
    #:
    #: 実測44〜45回目、レジュメAPIが3回とも **1バイト違わない** 5万字のHTMLを
    #: 返した。ヘッダを足しても長さが動かないのは、要求がハンドラに届いていない
    #: 形である。転送されていれば ``HTTP 200 + HTML`` はそのまま説明が付くが、
    #: **測っていなかったので分からなかった**。
    #:
    #: 空なら「転送されたかどうかが分からない」であって「転送されていない」では
    #: ない。区別は :meth:`was_redirected` が持つ。
    final_url: str = ""

    def was_redirected(self, requested_url: str) -> bool | None:
        """Whether the response came from somewhere else. ``None`` if unknown.

        **3値である。** 「転送された」「されていない」「分からない」を同じ言葉に
        すると、測れていないことが「問題なし」として現れる (原則2)。
        """
        if not self.final_url:
            return None
        return self.final_url.split("?", 1)[0] != requested_url.split("?", 1)[0]

    def json_body(self) -> Mapping[str, object] | None:
        """Parse the body as a JSON object, or ``None``.

        10.6: 応答がJSONとは限らない。パースに失敗しても例外にせず、呼び出し側が
        構造ダイジェストへ落とせるようにする。
        """
        try:
            parsed = json.loads(self.body_text)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class HttpRequest:
    """A request as it was actually issued. Contract tests assert on these."""

    method: str
    url: str
    headers: Mapping[str, str]
    json_body: Mapping[str, object] | None


class HttpTransport(Protocol):
    """Anything that can issue an authenticated request to the platform."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object] | None = None,
    ) -> HttpResponse: ...


class RecordedTransport:
    """A scripted transport for tests. Records requests, replays responses.

    送信APIの契約 (URL・必須ヘッダ・payload形状・成功ステータス) を、ネットワーク
    なしで固定するために使う。実際の応答は段階3の偵察が記録したものを流し込む。
    """

    def __init__(self, responses: Sequence[HttpResponse] | None = None) -> None:
        self._responses: list[HttpResponse] = list(responses or ())
        self.requests: list[HttpRequest] = []

    def queue(self, response: HttpResponse) -> None:
        self._responses.append(response)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object] | None = None,
    ) -> HttpResponse:
        self.requests.append(
            HttpRequest(method=method, url=url, headers=dict(headers), json_body=json)
        )
        if not self._responses:
            raise AssertionError(
                f"RecordedTransport に応答が残っていません (要求: {method} {url})。"
                f"テストが想定より多くのリクエストを発行しています。"
            )
        return self._responses.pop(0)

    @property
    def last_request(self) -> HttpRequest:
        if not self.requests:
            raise AssertionError("リクエストがまだ発行されていません")
        return self.requests[-1]

    @property
    def request_count(self) -> int:
        return len(self.requests)
