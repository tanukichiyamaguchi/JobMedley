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
