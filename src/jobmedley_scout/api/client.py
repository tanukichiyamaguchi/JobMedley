"""The platform API client.

6.6 の事故が本モジュールの中心にある:

> パスワード期限切れ時に全APIがエラーを返していたにもかかわらず、各メソッドが
> 警告ログを出して空の値を返すだけだったため、**CIは成功 (緑) のまま送信0件が
> 続きました。**

対処:

* 認証切れ専用の例外型 (:class:`PermanentAuthError`) を **送出する** (返さない)
* 判定条件は保守的にする -- 「401、または403かつエラーコードが認証系」。
  単発の権限エラーで全体を落とさないため
* 例外を握りつぶす箇所すべてで、この例外型だけは再送出する
* 上位まで伝播させ、終了コードを非0にする

**媒体固有の失効応答フォーマットは実測する** (座標 ``api.auth_failure_codes``)。
汎用の401判定だけでは、403＋独自コードの形式を取り逃す。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from jobmedley_scout.api.endpoints import Endpoint
from jobmedley_scout.api.success import graphql_errors, is_success
from jobmedley_scout.api.transport import HttpResponse, HttpTransport
from jobmedley_scout.config.placeholders import Coord, is_resolved, require
from jobmedley_scout.errors import PermanentAuthError

HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403

#: 認証切れを示す語の既定セット。座標 ``api.auth_failure_codes`` が実測値で
#: 上書きする。**推測に頼らない** -- ここはあくまで座標が未確定な間の保険。
FALLBACK_AUTH_CODE_HINTS: frozenset[str] = frozenset(
    {"unauthorized", "unauthenticated", "session_expired", "invalid_session", "login_required"}
)


@dataclass(frozen=True)
class ApiOutcome:
    """The result of one API call that did *not* raise."""

    endpoint_id: str
    status: int
    succeeded: bool
    response: HttpResponse

    def json_body(self) -> Mapping[str, object] | None:
        return self.response.json_body()


def classify_auth_failure(
    status: int, body: Mapping[str, object] | None, auth_codes: frozenset[str]
) -> str | None:
    """Return the auth-failure code if this response means the session is dead.

    **保守的に判定する** (6.6): 401 は無条件、403 は認証系のコードを伴う場合のみ。
    403 を無条件に認証切れとみなすと、単発の権限エラー (この候補者には送れない等)
    で実行全体が落ちる。

    **GraphQL の失効は 401 でも 403 でも来ない。** 実測20回目で分かったとおり、
    媒体の送信は GraphQL の mutation である。GraphQL は失効を HTTP 200 で返し、
    本文の ``errors[].extensions.code`` に ``UNAUTHENTICATED`` のようなコードを
    入れる。ステータスコードだけを見る判定では **これが1件も引っかからない**。

    引っかからないと何が起きるか。セッションが死んでいるのに例外が上がらず、
    全件が「失敗」として静かに積み上がる -- 6.6 の事故 (CIは緑のまま送信0件) が
    そのまま再現する。だから **本文のエラーコードも同じ集合で照合する**。
    """
    if status == HTTP_UNAUTHORIZED:
        return "http_401"
    # **ステータスが正常でも、本文が失効を訴えていることがある** (GraphQL)。
    for code in graphql_errors(body):
        needle = code.lower()
        if needle and any(hint in needle or needle in hint for hint in auth_codes):
            return code
    if status != HTTP_FORBIDDEN:
        return None
    if body is None:
        # 403 だが本文が読めない。**認証切れと断定しない** -- 保守的側に倒す。
        return None
    haystack = " ".join(
        str(value).lower() for value in body.values() if isinstance(value, str | int | float)
    )
    code_field = str(body.get("code", "")).lower()
    for code in auth_codes:
        needle = code.lower()
        if needle and (needle in haystack or needle == code_field):
            return code
    return None


class JobMedleyApiClient:
    """Issues authenticated requests and classifies permanent auth failures.

    12.5: **送信APIには自動リトライを掛けない。** 本クライアントはリトライを一切
    実装していない。これは意図的である -- 送信APIへ「親切にリトライを足す」と
    二重送信事故に直結する。冪等キーの事前永続化 (9.2) と次回実行に委ねること。
    リトライ方針は :mod:`runtime.retry` に層ごとに宣言してある。
    """

    def __init__(
        self,
        transport: HttpTransport,
        *,
        auth_failure_codes: Coord[tuple[str, ...]],
        idempotency_header: Coord[str | None],
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._extra_headers = dict(extra_headers or {})
        self._idempotency_header = idempotency_header
        if is_resolved(auth_failure_codes):
            codes = require(auth_failure_codes, used_by="api.client.__init__")
            self._auth_codes = frozenset(code.lower() for code in codes)
        else:
            # 座標未確定。汎用の保険だけで動かすが、401 は必ず捕まえる。
            # 403＋独自コードは取り逃す可能性があるので、段階3で実測すること。
            self._auth_codes = FALLBACK_AUTH_CODE_HINTS

    def _headers(self, idempotency_key: str | None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._extra_headers}
        if idempotency_key is not None and is_resolved(self._idempotency_header):
            header_name = require(self._idempotency_header, used_by="api.client._headers")
            if header_name:
                headers[header_name] = idempotency_key
        return headers

    def call(
        self,
        endpoint: Endpoint,
        *,
        url: str,
        json_body: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> ApiOutcome:
        """Issue one request. Raises :class:`PermanentAuthError` on a dead session."""
        response = self._transport.request(
            endpoint.method, url, headers=self._headers(idempotency_key), json=json_body
        )
        auth_code = classify_auth_failure(response.status, response.json_body(), self._auth_codes)
        if auth_code is not None:
            # **返さずに送出する。** 空の値を返して警告ログを出すのが 6.6 の事故。
            raise PermanentAuthError(
                f"媒体セッションが失効しています (endpoint={endpoint.id})。"
                f"保存セッションを再取得してシークレットを更新してください。",
                status=response.status,
                code=auth_code,
            )
        return ApiOutcome(
            endpoint_id=endpoint.id,
            status=response.status,
            succeeded=is_success(endpoint, response.status, response.json_body()),
            response=response,
        )
