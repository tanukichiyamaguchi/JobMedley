"""3本目の失敗経路 -- 応答本文の ``errorMessage`` 欄。

実測31回目 (follow-send) で、送信の mutation 文書そのものを観測した::

    mutation SendSingleScout($input: MessageScoutSendInput!) {
      result: messageScoutSend(input: $input) {
        scoutedMemberId
        errorMessage        <- **これ**
        __typename
      }
    }

**この欄は HTTP ステータスも ``errors`` 配列も通り抜ける。** HTTP 200・errors 無し・
それでも送れていない応答がありうる。媒体がわざわざ選択集合に入れている以上、
埋まる場面が在るということである。

見落とすと何が起きるか。成功として記録された候補者は重複送信の防止が効いて
二度と対象にならない -- **送っていないのに、送ったことになる。** 取りこぼしが
静かに永続化する、原則2 の最悪の形である。
"""

from __future__ import annotations

from jobmedley_scout.api.endpoints import Endpoint
from jobmedley_scout.api.success import (
    describe_failure,
    describe_status,
    graphql_errors,
    is_success,
    payload_error_paths,
)

SEND = Endpoint(
    id="send.paid",
    method="POST",
    slot="paid",
    url_pattern="https://example.invalid/api/customers/graphql/SendSingleScout",
    success_statuses=frozenset({200}),
    side_effectful=True,
)

#: 観測した選択集合そのままの形。**送れている** 側。
SENT = {"data": {"result": {"scoutedMemberId": "1", "errorMessage": None, "__typename": "X"}}}

#: 同じ形で、業務エラーだけが入っている。**HTTP は 200、errors 配列は無い。**
NOT_SENT = {
    "data": {
        "result": {
            "scoutedMemberId": None,
            "errorMessage": "スカウトの送信上限に達しています",
            "__typename": "X",
        }
    }
}


def test_an_error_message_at_http_200_is_not_counted_as_a_send() -> None:
    """**これが3本目の経路。1本目も2本目も素通りする。**"""
    assert is_success(SEND, 200, SENT) is True
    assert graphql_errors(NOT_SENT) == ()  # 2本目には映らない
    assert is_success(SEND, 200, NOT_SENT) is False


def test_a_null_error_message_is_not_an_error() -> None:
    """成功時は欄が在って ``null``。**在ること自体を失敗の合図にしない。**"""
    assert payload_error_paths(SENT) == ()
    assert payload_error_paths({"data": {"result": {"errorMessage": ""}}}) == ()
    assert payload_error_paths({"data": {"result": {"errorMessage": "   "}}}) == ()


def test_the_path_is_reported_but_never_the_text() -> None:
    """媒体の文言には候補者名が混ざりうる (13.2)。**キーパスだけを持ち回る。**"""
    leaky = {
        "data": {"result": {"errorMessage": "山田太郎さん (会員番号 03323741) へは送れません"}}
    }
    assert payload_error_paths(leaky) == ("data.result.errorMessage",)
    said = describe_status(SEND, 200, leaky)
    assert "山田" not in said
    assert "03323741" not in said
    assert "data.result.errorMessage" in said
    assert "失敗" in said


def test_an_unexpected_shape_falls_to_the_failure_side() -> None:
    """**形はまだ観測していない** (失敗応答を1度も見ていない)。

    文字列だと決めつけた判定を書くと、想定外の形が来たときに黙って成功へ倒れる。
    埋まっていたら形を問わず失敗にする -- 逆向きの間違いだけは犯さない。
    """
    assert payload_error_paths({"data": {"r": {"errorMessage": ["上限"]}}}) == (
        "data.r.errorMessage",
    )
    assert payload_error_paths({"data": {"r": {"errorMessage": {"code": "X"}}}}) == (
        "data.r.errorMessage",
    )
    assert payload_error_paths({"data": {"r": {"errorMessage": []}}}) == ()


def test_only_the_data_subtree_is_searched() -> None:
    """``errors`` 配列は :func:`graphql_errors` の担当。**二重に数えない。**"""
    both = {
        "data": {"result": {"errorMessage": "だめ"}},
        "errors": [{"extensions": {"code": "SOME_CODE"}}],
    }
    assert payload_error_paths(both) == ("data.result.errorMessage",)
    assert graphql_errors(both) == ("SOME_CODE",)
    assert describe_status(SEND, 200, both).count("errorMessage") == 1


def test_responses_without_the_field_are_unaffected() -> None:
    """読み取りの応答も同じ関数を通る。**そこを壊さない。**"""
    assert payload_error_paths({"data": {"members": [{"id": 1}, {"id": 2}]}}) == ()
    assert payload_error_paths(None) == ()
    assert payload_error_paths({}) == ()
    assert is_success(SEND, 200, {"data": {"members": []}}) is True


def test_nested_payloads_are_reached() -> None:
    """観測した深さは2だが、上限まで潜る。**深いところの失敗も数える。**"""
    nested = {"data": {"a": {"b": {"c": {"errorMessage": "だめ"}}}}}
    assert payload_error_paths(nested) == ("data.a.b.c.errorMessage",)
    listed = {"data": {"results": [{"errorMessage": None}, {"errorMessage": "だめ"}]}}
    assert payload_error_paths(listed) == ("data.results[1].errorMessage",)


def test_a_failure_reason_never_reads_as_a_bare_http_200() -> None:
    """**「HTTP 200」とだけ書かれた失敗記録を残さない。**

    送信は GraphQL なので失敗もステータスは 200 で来る。理由が「HTTP 200」だけの
    記録は、読んだ人間に成功と区別がつかない。レポートが起きたことと食い違うのは、
    それ自体が事故である。
    """
    assert describe_failure(SEND, 200, SENT) is None
    reason = describe_failure(SEND, 200, NOT_SENT)
    assert reason is not None
    assert reason != "HTTP 200"
    assert "data.result.errorMessage" in reason
    assert "上限" not in reason  # 文言は出さない (13.2)


def test_a_failure_reason_names_which_channel_failed() -> None:
    """3本のどれで落ちたかが分かること。**内訳が読めない記録は役に立たない。**"""
    by_status = describe_failure(SEND, 500, None)
    assert by_status is not None
    assert "HTTP 500" in by_status

    by_errors = describe_failure(SEND, 200, {"errors": [{"extensions": {"code": "LIMIT"}}]})
    assert by_errors is not None
    assert "LIMIT" in by_errors
