"""**本文を持つ要求を GET で送らない。** 実測46回目、5回分の遠回りの原因。

レジュメの経路が ``method="GET"`` のまま GraphQL の POST を呼んでいた。ルートに
当たらず、媒体は 404 ページへ転送する。返るのは **HTTP 200 の HTML** である。

    読めなかった: HTTP 200 / content-type: text/html / 長さ 51976 字
                  **転送されました** -> https://customers.job-medley.com/customers/404/

3回とも1バイト違わなかったのも、ヘッダを5回足しても動かなかったのも、
**要求がハンドラに届いていなかった**からである。

同じ間違いは隣の ``CANDIDATE_LIST`` で 2026-08-21 に直してあり、注記には予言まで
書いてあった。

    **GET ではない。** …GET のまま呼べば当たらない -- しかも 404/405 は
    「候補者0件」と区別が付かない形で上流に伝わりうる (原則2)。

**書いてあっても、隣は直らなかった。** だから注記ではなく構造で止める。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobmedley_scout.api.client import BODYLESS_METHODS, JobMedleyApiClient
from jobmedley_scout.api.endpoints import (
    CANDIDATE_LIST,
    RESUME,
    SEND_PAID,
    Endpoint,
    build_endpoints,
)
from jobmedley_scout.api.transport import HttpResponse, RecordedTransport
from jobmedley_scout.config.loader import load_site_coordinates
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.send_record import SendSlot

COORDINATES = load_site_coordinates(Path("config/site_coordinates.yaml"))

#: 要求本文を持つエンドポイント。**座標に payload_template がある = POST である。**
WITH_A_BODY = (CANDIDATE_LIST, RESUME, SEND_PAID)


def _client(transport: RecordedTransport) -> JobMedleyApiClient:
    return JobMedleyApiClient(
        transport,
        auth_failure_codes=COORDINATES.string_list("api.auth_failure_codes"),
        idempotency_header=COORDINATES.optional_string("api.idempotency_header"),
    )


@pytest.mark.parametrize("endpoint_id", WITH_A_BODY)
def test_an_endpoint_with_a_payload_template_is_declared_post(endpoint_id: str) -> None:
    """**座標に本文の雛形があるなら、それは POST である。**

    この媒体は送信が GraphQL、読み取りが REST の POST で、本文を持たない経路が
    そもそも無い。GET と宣言されていたら、それは参照実装からの引き写しである。
    """
    endpoint = build_endpoints(COORDINATES)[endpoint_id]
    assert endpoint.method == "POST", (
        f"{endpoint_id} が {endpoint.method} と宣言されています。"
        f"本文を持つ要求は GET では送れません -- ルートに当たらず 404 になり、"
        f"それが「0件」として現れます (実測46回目)。"
    )


def test_sending_a_body_on_a_get_is_refused_before_the_request_goes_out() -> None:
    """**構造で止める。** 注記は隣のエンドポイントを直してくれなかった。

    404 は「0件」と区別が付かない形で上流に伝わる。呼ぶ前に止めれば、報告は
    「取れませんでした」ではなく「宣言が違います」になる。
    """
    broken = Endpoint(
        id="broken",
        method="GET",
        url_pattern="https://example.test/api/thing",
        success_statuses=frozenset({200}),
        slot=SendSlot.UNKNOWN,
        side_effectful=False,
    )
    transport = RecordedTransport([HttpResponse(status=200, body_text="{}")])
    with pytest.raises(ConfigError) as caught:
        _client(transport).call(broken, url="https://example.test/api/thing", json_body={"a": 1})
    assert "GET では送れません" in str(caught.value)
    assert transport.requests == [], "止めたと言いながら媒体へ呼びに行っています"


def test_a_get_without_a_body_is_still_allowed() -> None:
    """**本文の無い GET は正常である。** 広く止めると読み取りが全部落ちる。"""
    readonly = Endpoint(
        id="readonly",
        method="GET",
        url_pattern="https://example.test/api/quota",
        success_statuses=frozenset({200}),
        slot=SendSlot.UNKNOWN,
        side_effectful=False,
    )
    transport = RecordedTransport([HttpResponse(status=200, body_text='{"remaining": 3}')])
    outcome = _client(transport).call(readonly, url="https://example.test/api/quota")
    assert outcome.succeeded


def test_head_is_treated_like_get() -> None:
    assert BODYLESS_METHODS == frozenset({"GET", "HEAD"})
