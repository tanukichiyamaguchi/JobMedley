"""「この GraphQL は読み取りだけか」の判定を固定する。

**この判定を誤ると、取り消せない送信が通る。** だからここで守るのは一点だけ:

**判定できないものは全て「通さない」に倒れること。**

読み取りを1本止め損ねても画面が開かないだけで復旧できる。書き込みを1本
通してしまうと復旧できない。テストはその非対称性に沿って書いてある。
"""

from __future__ import annotations

import json

from jobmedley_scout.recon.graphql import is_read_only_graphql

GRAPHQL_URL = "https://customers.job-medley.com/api/customers/graphql/MemberOnScoutProfile"


def _body(document: str, **variables: object) -> str:
    return json.dumps({"operationName": "X", "query": document, "variables": variables})


# --- 通すもの ------------------------------------------------------------------


def test_a_named_query_passes() -> None:
    """実測5回目で止めてしまったのがこれ。止めると画面が開かない。"""
    assert is_read_only_graphql(GRAPHQL_URL, _body("query MemberOnScoutProfile { member { id } }"))


def test_an_anonymous_query_passes() -> None:
    """``{ ... }`` は仕様上 query である。"""
    assert is_read_only_graphql(GRAPHQL_URL, _body("{ member { id } }"))


def test_a_batched_request_of_queries_passes() -> None:
    payload = json.dumps(
        [
            {"query": "query A { a }"},
            {"query": "query B { b }"},
        ]
    )
    assert is_read_only_graphql(GRAPHQL_URL, payload)


def test_the_word_mutation_inside_a_string_literal_does_not_block() -> None:
    """文字列の中の ``mutation`` は操作ではない。"""
    document = 'query Search { members(keyword: "mutation") { id } }'
    assert is_read_only_graphql(GRAPHQL_URL, _body(document))


# --- 止めるもの (ここが安全そのもの) --------------------------------------------


def test_a_mutation_never_passes() -> None:
    """**スカウト送信はここに来る。** 通ったら取り消せない。"""
    document = "mutation SendScout($input: ScoutInput!) { sendScout(input: $input) { id } }"
    assert not is_read_only_graphql(GRAPHQL_URL, _body(document))


def test_a_batch_that_hides_one_mutation_never_passes() -> None:
    """**1つでも書き込みが混ざれば全体を止める。** まとめ送りは抜け道になりうる。"""
    payload = json.dumps(
        [
            {"query": "query A { a }"},
            {"query": "mutation SendScout { sendScout { id } }"},
        ]
    )
    assert not is_read_only_graphql(GRAPHQL_URL, payload)


def test_a_subscription_never_passes() -> None:
    assert not is_read_only_graphql(GRAPHQL_URL, _body("subscription S { onScout { id } }"))


def test_a_non_graphql_url_never_passes() -> None:
    """``query`` という項目を持つ普通の REST API を読み取り扱いしない。

    URL の条件は **2つ目の独立した鍵** である。片方だけでは開かない。
    """
    assert not is_read_only_graphql(
        "https://customers.job-medley.com/api/customers/members/mark_read/",
        _body("query A { a }"),
    )


def test_an_unparseable_body_never_passes() -> None:
    assert not is_read_only_graphql(GRAPHQL_URL, "not json at all")


def test_a_missing_or_empty_body_never_passes() -> None:
    assert not is_read_only_graphql(GRAPHQL_URL, None)
    assert not is_read_only_graphql(GRAPHQL_URL, "")


def test_a_payload_without_a_query_field_never_passes() -> None:
    assert not is_read_only_graphql(GRAPHQL_URL, json.dumps({"variables": {"id": 1}}))
    assert not is_read_only_graphql(GRAPHQL_URL, json.dumps({"query": ""}))
    assert not is_read_only_graphql(GRAPHQL_URL, json.dumps({"query": 42}))


def test_an_empty_batch_never_passes() -> None:
    """空の配列は「何も書き込まない」ではなく「判定できない」。"""
    assert not is_read_only_graphql(GRAPHQL_URL, json.dumps([]))


def test_a_document_that_is_neither_keyword_nor_braces_never_passes() -> None:
    """形が読めないものは通さない。"""
    assert not is_read_only_graphql(GRAPHQL_URL, _body("sendScout(input: {}) { id }"))
