"""The send-path contract, fixed without a network or a browser.

13.4: 「API層はHTTPクライアントを差し替え可能にし、URL・ヘッダ・リクエストボディの
契約を **ネットワークなしで固定するテスト** を書く。**送信APIは契約テストが唯一の
防波堤です。**」

ここで固定するのは、段階3の偵察と段階4のdryRun検証で **実測して確定した** 契約である。
現時点ではジョブメドレーの実値がまだ無いので、値そのものは仮のものを使い、
**契約の形** (URLの組み立て方・冪等キーの載り方・payload雛形の差し込み・
エンドポイントごとの成功判定) を固定している。実値が確定したら、このテストの
定数を実測値に置き換えることで回帰テストになる (13.4: 実データで確定した仕様は
回帰テストに落とす)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jobmedley_scout.api.client import JobMedleyApiClient
from jobmedley_scout.api.endpoints import SEND_PAID, build_endpoints
from jobmedley_scout.api.send import send_message
from jobmedley_scout.api.success import is_success
from jobmedley_scout.api.transport import HttpResponse, RecordedTransport
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.errors import PermanentAuthError
from jobmedley_scout.models.message import AssembledMessage
from jobmedley_scout.models.send_record import MessageKind, ReservedSend, SendSlot

IDEMPOTENCY_HEADER = "X-Idempotency-Key"

#: 偵察が記録した実リクエストボディを雛形にする、という運用を模したもの。
#: 候補者IDの位置・件名の位置・本文の位置がプレースホルダになっている。
PAYLOAD_TEMPLATE = json.dumps(
    {
        "recipient": {"id": "{{CANDIDATE_ID}}"},
        "subject": "{{SUBJECT}}",
        "body": "{{BODY}}",
        "followup": None,
    },
    ensure_ascii=False,
)


def _coordinates(**overrides: object) -> SiteCoordinates:
    """A fully-resolved coordinate set for contract testing."""
    values: dict[str, object] = {
        "api.base_url": "https://customers.example.test",
        "api.candidate_list.url_pattern": "https://customers.example.test/api/candidates",
        "api.resume.url_pattern": "https://customers.example.test/api/resume/{{CANDIDATE_ID}}",
        "api.precheck.url_pattern": None,
        "api.quota.url_pattern": None,
        "api.idempotency_header": IDEMPOTENCY_HEADER,
        "api.send.paid.url_pattern": "https://customers.example.test/api/scout/{{CANDIDATE_ID}}",
        # 6.2: **成功ステータスはエンドポイントごとに違う。** ここでは 201 のみ成功。
        "api.send.paid.success_statuses": frozenset({201}),
        "api.send.paid.payload_template": PAYLOAD_TEMPLATE,
        "api.send.free.url_pattern": None,
        "api.send.free.success_statuses": None,
        "api.send.free.payload_template": None,
        "api.auth_failure_codes": ("session_expired", "invalid_session"),
    }
    values.update(overrides)
    return SiteCoordinates(values)


def _reserved() -> ReservedSend:
    return ReservedSend(
        record_id=1,
        candidate_id="CAND-001",
        idempotency_key="idem-abc-123",
        message_kind=MessageKind.FIRST_CONTACT,
        followup_seq=0,
        slot=SendSlot.PAID,
        subject="ご経験を拝見してご連絡しました",
        reserved_at=datetime(2026, 8, 11, tzinfo=UTC),
    )


def _message() -> AssembledMessage:
    subject = "ご経験を拝見してご連絡しました"
    return AssembledMessage(
        subject=subject,
        body="本文です。",
        subject_norm=subject,
        subject_prefix35=subject[:35],
    )


def _client(transport: RecordedTransport, coords: SiteCoordinates) -> JobMedleyApiClient:
    return JobMedleyApiClient(
        transport,
        auth_failure_codes=coords.string_list("api.auth_failure_codes"),
        idempotency_header=coords.optional_string("api.idempotency_header"),
    )


def test_send_request_shape_is_fixed() -> None:
    """URL・メソッド・ヘッダ・payload の契約。"""
    coords = _coordinates()
    endpoints = build_endpoints(coords)
    transport = RecordedTransport([HttpResponse(status=201, body_text='{"id": "msg-9"}')])
    result = send_message(
        _client(transport, coords),
        endpoints[SEND_PAID],
        _reserved(),
        _message(),
        payload_template=coords.json_path("api.send.paid.payload_template"),
    )

    request = transport.last_request
    assert request.method == "POST"
    # 候補者IDがURLに差し込まれている。
    assert request.url == "https://customers.example.test/api/scout/CAND-001"
    # **ブラウザから観測した形に揃えてある** (実測42回目)。
    #
    # 長く "application/json" だけを送っていた。ブラウザは charset を付け、
    # Accept と Accept-Language と Origin も付ける。どれも観測した値であり、
    # 観測した値を写すことは推測ではない (原則3)。
    assert request.headers["Content-Type"] == "application/json;charset=UTF-8"
    assert request.headers["Accept"] == "application/json, text/plain, */*"
    assert request.headers["Accept-Language"] == "ja-JP,ja;q=0.9,en;q=0.8"
    # Origin は要求先から作る。座標に書き起こすと、URLを変えたとき黙って食い違う。
    assert request.headers["Origin"] == "https://customers.example.test"
    # 9.2: 冪等キーが載る。これが無いと再試行がサーバ側で重複排除されない。
    assert request.headers[IDEMPOTENCY_HEADER] == "idem-abc-123"

    assert request.json_body is not None
    assert request.json_body["recipient"] == {"id": "CAND-001"}
    assert request.json_body["subject"] == "ご経験を拝見してご連絡しました"
    assert request.json_body["body"] == "本文です。"
    # 6.7: オプショナルなネスト構造は null のまま。文字列を入れると全滅する。
    assert request.json_body["followup"] is None

    assert result.succeeded
    # 9.4: 送信枠とエンドポイントは結果に必ず載る。後から復元できないため。
    assert result.slot is SendSlot.PAID
    assert result.endpoint_id == SEND_PAID
    assert result.idempotency_key == "idem-abc-123"


def test_success_status_is_per_endpoint_not_hardcoded_200() -> None:
    """6.2: 200 のみを成功とみなす実装では、成功しているのに失敗扱いになる。

    このエンドポイントの成功は 201 のみと座標で宣言してあるので、
    200 は **失敗** と判定されなければならない。
    """
    coords = _coordinates()
    endpoint = build_endpoints(coords)[SEND_PAID]
    assert is_success(endpoint, 201) is True
    assert is_success(endpoint, 200) is False


def test_non_success_status_reports_failure_without_raising() -> None:
    """確定失敗は例外ではなく結果として返る (呼び出し側が状態遷移を決めるため)。"""
    coords = _coordinates()
    endpoints = build_endpoints(coords)
    transport = RecordedTransport([HttpResponse(status=422, body_text='{"message": "bad"}')])
    result = send_message(
        _client(transport, coords),
        endpoints[SEND_PAID],
        _reserved(),
        _message(),
        payload_template=coords.json_path("api.send.paid.payload_template"),
    )
    assert not result.succeeded
    assert result.http_status == 422
    # 失敗でも枠とエンドポイントは載る -- 失敗の内訳も枠ごとに見たいから。
    assert result.slot is SendSlot.PAID


def test_a_send_that_only_says_errorMessage_is_not_recorded_as_sent() -> None:
    """**3本目の失敗経路** (2026-08-23 実測31回目)。

    実測した mutation の選択集合には ``errorMessage`` が在る::

        result: messageScoutSend(input: $input) {
          scoutedMemberId
          errorMessage
          __typename
        }

    ここへ文言が入った応答は、**成功ステータスで来て、errors 配列も空である**。
    見落とすと送信済みとして記録され、その候補者は重複送信の防止に掛かって二度と
    対象にならない -- **送っていないのに送ったことになる** (原則2)。
    """
    coords = _coordinates()
    endpoints = build_endpoints(coords)
    body = json.dumps(
        {
            "data": {
                "result": {
                    "scoutedMemberId": None,
                    "errorMessage": "スカウトの送信上限に達しています",
                    "__typename": "MessageScoutSendPayload",
                }
            }
        },
        ensure_ascii=False,
    )
    # **成功ステータス** (この座標では 201) で返す。1本目も2本目も通り抜ける形。
    transport = RecordedTransport([HttpResponse(status=201, body_text=body)])
    result = send_message(
        _client(transport, coords),
        endpoints[SEND_PAID],
        _reserved(),
        _message(),
        payload_template=coords.json_path("api.send.paid.payload_template"),
    )
    assert not result.succeeded
    assert result.slot is SendSlot.PAID


def test_a_failure_reason_says_more_than_the_status_code() -> None:
    """**「HTTP 201」とだけ書かれた失敗記録を残さない。**

    送信は GraphQL なので、失敗も成功ステータスで来る。理由がステータスだけの
    記録は、読んだ人間に成功と区別がつかず、原因も分からない。
    あわせて **媒体の文言は記録しない** (13.2) -- 候補者名が混ざりうる。
    """
    coords = _coordinates()
    endpoints = build_endpoints(coords)
    body = json.dumps(
        {"data": {"result": {"errorMessage": "山田太郎さん (会員番号 03323741) へは送れません"}}},
        ensure_ascii=False,
    )
    transport = RecordedTransport([HttpResponse(status=201, body_text=body)])
    result = send_message(
        _client(transport, coords),
        endpoints[SEND_PAID],
        _reserved(),
        _message(),
        payload_template=coords.json_path("api.send.paid.payload_template"),
    )
    assert result.failure_reason is not None
    assert result.failure_reason != "HTTP 201"
    assert "data.result.errorMessage" in result.failure_reason
    assert "山田" not in result.failure_reason
    assert "03323741" not in result.failure_reason
    # 会員IDをメッセージIDとして詰めない (応答にメッセージIDは無い)。
    assert result.platform_message_id is None


def test_401_raises_permanent_auth_error() -> None:
    """6.6: 認証切れは **送出する**。空の値を返して警告ログを出すのが事故の原因。"""
    coords = _coordinates()
    endpoints = build_endpoints(coords)
    transport = RecordedTransport([HttpResponse(status=401, body_text="{}")])
    with pytest.raises(PermanentAuthError) as excinfo:
        send_message(
            _client(transport, coords),
            endpoints[SEND_PAID],
            _reserved(),
            _message(),
            payload_template=coords.json_path("api.send.paid.payload_template"),
        )
    assert excinfo.value.status == 401


def test_403_with_auth_code_raises_but_bare_403_does_not() -> None:
    """6.6: 判定は保守的に。403 は認証系コードを伴う場合のみ認証切れとみなす。

    403 を無条件に認証切れとすると、「この候補者には送れない」程度の単発の権限
    エラーで実行全体が落ちる。
    """
    coords = _coordinates()
    endpoints = build_endpoints(coords)

    # 認証系コードつき -> 認証切れ
    transport = RecordedTransport(
        [HttpResponse(status=403, body_text='{"code": "session_expired"}')]
    )
    with pytest.raises(PermanentAuthError):
        send_message(
            _client(transport, coords),
            endpoints[SEND_PAID],
            _reserved(),
            _message(),
            payload_template=coords.json_path("api.send.paid.payload_template"),
        )

    # ただの権限エラー -> 実行全体は落とさず、失敗として返す
    transport = RecordedTransport([HttpResponse(status=403, body_text='{"code": "not_eligible"}')])
    result = send_message(
        _client(transport, coords),
        endpoints[SEND_PAID],
        _reserved(),
        _message(),
        payload_template=coords.json_path("api.send.paid.payload_template"),
    )
    assert not result.succeeded


def test_send_issues_exactly_one_request() -> None:
    """12.5: **送信APIには自動リトライを掛けない。**

    失敗しても1回きり。冪等キーの事前永続化と次回実行に委ねる。
    ここでリトライが入ると二重送信事故に直結する。
    """
    coords = _coordinates()
    endpoints = build_endpoints(coords)
    transport = RecordedTransport([HttpResponse(status=500, body_text="{}")])
    send_message(
        _client(transport, coords),
        endpoints[SEND_PAID],
        _reserved(),
        _message(),
        payload_template=coords.json_path("api.send.paid.payload_template"),
    )
    assert transport.request_count == 1


def test_unresolved_send_url_stops_explicitly() -> None:
    """座標未確定なら、黙って0件で成功せず **明示的に停止する** (原則2)。"""
    from jobmedley_scout.config.placeholders import LadderStage, Unresolved
    from jobmedley_scout.errors import UnresolvedCoordinateError

    coords = _coordinates(
        **{
            "api.send.paid.url_pattern": Unresolved(
                "api.send.paid.url_pattern", LadderStage.STAGE_3_RECON, "偵察の出力から転記"
            )
        }
    )
    endpoints = build_endpoints(coords)
    transport = RecordedTransport([HttpResponse(status=201, body_text="{}")])
    with pytest.raises(UnresolvedCoordinateError):
        send_message(
            _client(transport, coords),
            endpoints[SEND_PAID],
            _reserved(),
            _message(),
            payload_template=coords.json_path("api.send.paid.payload_template"),
        )
    # 未確定のまま1件も送っていないこと。
    assert transport.request_count == 0


def test_a_graphql_payload_without_a_query_document_is_refused() -> None:
    """**operationName と variables だけでは送れない。**

    2026-08-23 時点の api.send.paid.payload_template がまさにこれだった。
    ``assert_fully_filled`` は ``<...>`` の残りを見るが、**キーごと欠けている
    ものは見えない** -- 「埋まっている」と判定されたまま、サーバに拒否される
    payload が組み上がっていた。

    穴の由来は偵察側にある。あの雛形を記録した回は「長いから」という理由で
    query を落としていた。理由が「個人データだから」ではなく「長いから」
    だったので、13.2 ではなくこちらの都合である。
    """
    import pytest

    from jobmedley_scout.api.payloads import assert_sendable_graphql
    from jobmedley_scout.errors import ConfigError

    broken = {
        "operationName": "SendSingleScout",
        "variables": {"input": {"memberId": "1", "scoutMessage": "本文"}},
    }
    with pytest.raises(ConfigError, match="問い合わせ文"):
        assert_sendable_graphql(broken, used_by="test")


def test_a_graphql_payload_with_a_query_document_passes() -> None:
    from jobmedley_scout.api.payloads import assert_sendable_graphql

    ok = {
        "operationName": "SendSingleScout",
        "query": "mutation SendSingleScout($input: MessageScoutSendInput!) { x }",
        "variables": {"input": {}},
    }
    assert_sendable_graphql(ok, used_by="test")


def test_a_rest_payload_is_not_dragged_into_the_graphql_check() -> None:
    """**判定できないものは通す側へ倒す。**

    ここは送信の可否ではなく「明らかに送れない形」の門である。GraphQL でない
    payload まで撥ねると、REST の送信路を持つ媒体で門が誤作動する。
    """
    from jobmedley_scout.api.payloads import assert_sendable_graphql

    assert_sendable_graphql({"member_id": "1", "message": "本文"}, used_by="test")


def test_an_empty_query_string_is_refused_like_a_missing_one() -> None:
    """空文字は「在る」ではない。GraphQL は受け付けない。"""
    import pytest

    from jobmedley_scout.api.payloads import assert_sendable_graphql
    from jobmedley_scout.errors import ConfigError

    with pytest.raises(ConfigError, match="問い合わせ文"):
        assert_sendable_graphql(
            {"operationName": "SendSingleScout", "query": "   ", "variables": {}}, used_by="test"
        )
