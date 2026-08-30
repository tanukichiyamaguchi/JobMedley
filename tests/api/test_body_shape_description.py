"""**何であるかを言う。値は1文字も出さない** (13.2)。

実測39回目、レジュメが読めない理由が「HTTP 200 / 応答本文がオブジェクトでは
ありません」まで絞れたが、そこで止まった。オブジェクトでないことは分かっても、
HTMLなのか空なのかJSONの配列なのかが分からない。**次の手が決まらない報告** は
原則2 の言う静かなゼロの一段浅い版である。
"""

from __future__ import annotations

import json

import pytest

from jobmedley_scout.api.success import describe_body_shape

#: 本文に混ざっていたら事故になる文字列。候補者の氏名・会員番号のつもり。
SECRET = "3323741"


def test_a_login_page_is_named_as_html_so_the_next_step_is_obvious() -> None:
    """**これが分かれば十分。** 画面が返っているならセッションか経路の問題である。"""
    body = f"<!DOCTYPE html><html><body>ログインしてください {SECRET}</body></html>"
    described = describe_body_shape(body, {"content-type": "text/html; charset=utf-8"})
    assert "HTMLらしい" in described
    assert "text/html" in described
    assert SECRET not in described


def test_an_empty_body_is_named_rather_than_called_unreadable() -> None:
    assert "本文が空です" in describe_body_shape("   ")


def test_a_json_array_is_named_with_its_length_but_not_its_contents() -> None:
    described = describe_body_shape(json.dumps([{"code": SECRET}, {"code": "9"}]))
    assert "JSONの配列 (2 要素)" in described
    assert SECRET not in described


def test_json_null_is_not_confused_with_an_empty_body() -> None:
    """``null`` は「空」ではない。**媒体が意図して返した値である。**"""
    described = describe_body_shape("null")
    assert "JSONの null" in described
    assert "本文が空です" not in described


def test_a_json_scalar_says_its_type() -> None:
    assert "JSONだがオブジェクトではない (str)" in describe_body_shape(json.dumps(SECRET))
    assert SECRET not in describe_body_shape(json.dumps(SECRET))


def test_unparseable_non_html_is_distinguished_from_html() -> None:
    described = describe_body_shape(f"not json at all {SECRET}")
    assert "JSONとして読めません" in described
    assert "HTML" not in described
    assert SECRET not in described


def test_the_length_is_always_reported() -> None:
    """長さは形であって値ではない。**空と「短い」を分ける唯一の手がかり。**"""
    assert "長さ 4 字" in describe_body_shape("null")


@pytest.mark.parametrize(
    "headers", [None, {}, {"Content-Type": "application/json"}, {"content-type": "text/html"}]
)
def test_headers_are_optional_and_either_casing_is_read(headers: dict[str, str] | None) -> None:
    """見出しが無くても落ちない。**診断が例外になったら診断にならない。**"""
    described = describe_body_shape("[]", headers)
    assert "JSONの配列" in described


def test_a_body_that_is_an_object_still_gets_described() -> None:
    """呼ばれるのは「オブジェクトでなかった」経路だが、**関数は嘘をつかない。**"""
    described = describe_body_shape(json.dumps({"data": {"code": SECRET}}))
    assert SECRET not in described
