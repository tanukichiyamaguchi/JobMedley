"""**ブラウザが何を送っているかを観測する。** 実測41回目の宿題。

同じURLへ、ブラウザが POST すると JSON が返り、こちらが POST すると
**HTML が 51976 字返ってくる**::

    読めなかった: HTTP 200 / 応答本文がオブジェクトではありません
    (content-type: text/html / 長さ 51976 字 / JSONではなくHTMLらしい)

URL は実測済みで正しい。一覧APIは同じ経路・同じセッション・同じ
``Content-Type`` で JSON を返している。**違いは要求の側にある。**

こちらの ``api.client`` が付けているのは ``Content-Type`` だけである。
ブラウザが何を付けているかは、**見なければ決まらない**。推測で足すのは
原則3に反する -- 当たっても、当たったことを確かめる手段が無い。

**そして Cookie の値は絶対に出さない** (12.7)。出ればログを読んだ誰でも、
この運用者として媒体へ入れる。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.recon.api_shape import SAFE_HEADER_VALUES, describe_request_headers

#: セッションそのもの。**1文字も報告に出てはいけない。**
SESSION = "_jm_customer_session=SUPERSECRETVALUE123"


def test_the_cookie_value_never_reaches_the_report() -> None:
    """12.7。**これが漏れたら、誰でもこの運用者として媒体へ入れる。**"""
    described = describe_request_headers({"Cookie": SESSION, "Accept": "application/json"})
    joined = "\n".join(described)
    assert "SUPERSECRETVALUE123" not in joined
    assert "cookie: (値は伏せています)" in joined


@pytest.mark.parametrize("name", ["Authorization", "X-CSRF-Token", "Set-Cookie", "Referer"])
def test_other_credential_bearing_headers_are_name_only(name: str) -> None:
    """**許可した名前以外は全部伏せる。** 既定が安全側である。"""
    described = "\n".join(describe_request_headers({name: "秘密の値"}))
    assert "秘密の値" not in described
    assert f"{name.lower()}: (値は伏せています)" in described


def test_the_headers_that_answer_the_question_are_shown_with_their_values() -> None:
    """**差が引き算で出せること。** ここが観測の目的である。

    こちらが送っているのは ``Content-Type: application/json`` だけなので、
    ブラウザ側にこれ以外が並んでいれば、それが差である。
    """
    described = "\n".join(
        describe_request_headers(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Mode": "cors",
                "Cookie": SESSION,
            }
        )
    )
    assert "accept: application/json" in described
    assert "x-requested-with: XMLHttpRequest" in described
    assert "sec-fetch-mode: cors" in described
    assert SESSION not in described


def test_no_credential_header_is_on_the_safe_list() -> None:
    """**許可一覧そのものを固定する。** 増やすときに考える機会を作る。"""
    dangerous = {"cookie", "authorization", "x-csrf-token", "set-cookie", "proxy-authorization"}
    assert not (SAFE_HEADER_VALUES & dangerous)


def test_the_names_come_out_sorted_so_two_runs_can_be_compared() -> None:
    """並び順が実行ごとに変わると、差分を目で追えない。"""
    described = describe_request_headers({"Zeta": "1", "Alpha": "2", "Accept": "3"})
    assert [line.split(":")[0] for line in described] == ["accept", "alpha", "zeta"]


def test_no_headers_at_all_is_not_an_error() -> None:
    """観測できなかったことは、例外ではなく空で表す。**診断が落ちたら診断にならない。**"""
    assert describe_request_headers({}) == ()
