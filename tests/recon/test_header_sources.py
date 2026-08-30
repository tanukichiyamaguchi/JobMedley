"""**ブラウザが付けるヘッダの出所を、名前だけで探す。** 実測42回目の続き。

``observe-resume`` がブラウザの要求ヘッダを見せた。こちらが付けていないものが4つ::

    x-csrf-token / x-customer-user-id / x-customer-user-email / x-experiment-data

``x-csrf-token`` が無ければ POST は弾かれ、ログイン画面へ転送される -- 実測41回目に
レジュメAPIが返した5万字のHTMLの正体である。

**だが値の出所は分からない。** meta タグかもしれない。埋め込みJSONかもしれない。
当てにいけるが、当てても確かめる手段が無い (原則3)。だから名前だけを集めて並べ、
**どれを使うかは人間が決める**。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.recon.header_sources import (
    WANTED_HEADERS,
    find_sources,
    looks_like_a_source,
    search_terms,
)
from jobmedley_scout.recon.observe_headers import COLLECT_NAMES, HeaderObservation, HeaderStage


def test_the_wanted_headers_are_the_ones_the_browser_sends_and_we_do_not() -> None:
    """**実測した4つだけ。** 増やすときは観測してからにする。"""
    assert set(WANTED_HEADERS) == {
        "x-csrf-token",
        "x-customer-user-id",
        "x-customer-user-email",
        "x-experiment-data",
    }


@pytest.mark.parametrize(
    ("name", "header"),
    [
        ("csrf-token", "x-csrf-token"),
        ("csrfToken", "x-csrf-token"),
        ("meta[csrf-token]", "x-csrf-token"),
        ("customer-user-id", "x-customer-user-id"),
        ("customer_user_email", "x-customer-user-email"),
    ],
)
def test_a_plausible_source_is_offered(name: str, header: str) -> None:
    assert looks_like_a_source(name, header)


def test_two_headers_sharing_a_prefix_do_not_borrow_each_other_s_candidates() -> None:
    """**最後の部品を必須にした理由。**

    ``x-customer-user-id`` と ``x-customer-user-email`` は ``customer`` と ``user``
    を共有している。そこだけで当てると互いの候補に混ざる -- 実際に混ざった。
    """
    assert not looks_like_a_source("customer_user_email", "x-customer-user-id")
    assert not looks_like_a_source("customer-user-id", "x-customer-user-email")


@pytest.mark.parametrize("name", ["token", "id", "user", "viewport", "description"])
def test_a_word_that_appears_on_every_page_is_not_a_candidate(name: str) -> None:
    """広く拾いすぎると報告が候補で埋まり、本命が見えなくなる。"""
    assert not any(looks_like_a_source(name, header) for header in WANTED_HEADERS)


def test_a_header_with_no_candidate_is_named_rather_than_dropped() -> None:
    """**黙って諦めない** (原則2)。見つからなかったことも観測である。"""
    found = find_sources(["viewport", "description"])
    assert set(found.unresolved()) == set(WANTED_HEADERS)
    assert (
        "出所の候補が見つかりませんでした"
        in HeaderObservation(requested_url="https://example.test/", candidates=found).render()
    )


def test_the_report_says_how_many_names_were_seen() -> None:
    """0件と「探さなかった」を分ける唯一の手がかり。"""
    found = find_sources(["meta[csrf-token]", "viewport"])
    assert (
        "見た名前: 2 個"
        in HeaderObservation(requested_url="https://example.test/", candidates=found).render()
    )


def test_reading_nothing_is_not_reported_as_no_candidates() -> None:
    """**「読めなかった」と「無かった」は違う。** 打つ手が違う (原則2)。"""
    observation = HeaderObservation(
        requested_url="https://example.test/",
        candidates=find_sources([]),
        read_failed="TimeoutError",
    )
    assert observation.reached() is HeaderStage.NOTHING_READ
    rendered = observation.render()
    assert "1つも読めませんでした" in rendered
    assert "TimeoutError" in rendered


# ---------------------------------------------------------------------------
# 値を読む枝が存在しないこと
#
# **ここが 13.2 に対する一番強い保証である。** 「読まないようにした」ではなく
# 「読む書き方をしていない」ことを構文で確かめる。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    [
        "getAttribute('content')",
        'getAttribute("content")',
        "getItem",
        ".content",
        "innerText",
        ".click(",
        "dispatchEvent",
        "submit(",
    ],
)
def test_the_collector_has_no_way_to_read_a_value_or_press_anything(forbidden: str) -> None:
    """``x-csrf-token`` はそれだけで POST を通せる鍵である (12.7)。

    **押す枝も同じ検査で塞ぐ。** このモジュールは BLOCK_THIRD_PARTY を使う ので、
    媒体のオリジンは素通しになる。押さないことが安全性の全部であり、
    ``page.evaluate`` は原理的には押せる -- だからJSの中身を固定する (13.6)。

    ``x-customer-user-email`` は運用者のメールアドレスである。集めるJSに値を
    返す枝が1つも無ければ、後から「ついでに値も」と足されることもない。
    """
    assert forbidden not in COLLECT_NAMES


def test_the_matcher_cannot_receive_a_value_at_all() -> None:
    """**名前の配列しか受け取らない。** 値を渡せる引数が無い。"""
    import inspect

    assert list(inspect.signature(find_sources).parameters) == ["names"]


def test_search_terms_drop_the_x_prefix() -> None:
    assert "csrf-token" in search_terms("x-csrf-token")
