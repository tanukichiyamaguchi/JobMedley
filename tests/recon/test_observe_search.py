"""一覧の要求本文を観測するコマンドの、判断部分の検査。

ブラウザを開く部分は検査できない (13.4 のとおり実機の偵察で担保する)。ここで
見るのは **報告が嘘をつかないこと** だけである。
"""

from __future__ import annotations

import json

import pytest

from jobmedley_scout.recon.observe_search import (
    CapturedPost,
    SearchObservation,
    SearchStage,
    _SearchListener,
    match_path,
)
from jobmedley_scout.recon.search_payload import as_template

BODY = json.dumps(
    {
        "customer_search_condition_id": 739599,
        "member_id": [],
        "pagination": {"limit": 25, "page": 1},
    }
)


def _found() -> SearchObservation:
    return SearchObservation(
        requested_url="https://example.test/searches",
        listen_path="/api/customers/members/search",
        posts=(CapturedPost(status=200, body=BODY),),
        captured=as_template(BODY),
        listener_attached=True,
    )


def test_the_path_to_listen_for_comes_from_the_coordinate() -> None:
    """**綴りを作らない。** 座標が変われば聴く経路も変わる。"""
    assert (
        match_path("https://customers.job-medley.com/api/customers/members/search/")
        == "/api/customers/members/search"
    )


def test_a_url_without_a_path_falls_back_to_the_whole_value() -> None:
    """経路が取れないなら黙って空文字で照合しない -- 空文字は何にでも一致する。"""
    assert match_path("members/search") == "members/search"


def test_no_session_is_reported_as_no_session() -> None:
    observation = SearchObservation(
        requested_url="https://example.test/searches",
        listen_path="/x",
        session_present=False,
    )
    assert observation.reached() is SearchStage.NO_SESSION
    assert "保存セッションがありません" in observation.render()


def test_an_expired_session_is_not_reported_as_zero_posts() -> None:
    observation = SearchObservation(
        requested_url="https://example.test/searches",
        listen_path="/x",
        session_expired=True,
        listener_attached=True,
    )
    assert observation.reached() is SearchStage.SESSION_EXPIRED
    assert "セッションが切れています" in observation.render()


def test_hearing_nothing_is_named_rather_than_reported_as_success() -> None:
    """原則2。**聴けなかったことは、無かったことではない。**"""
    observation = SearchObservation(
        requested_url="https://example.test/searches",
        listen_path="/api/customers/members/search",
        listener_attached=True,
    )
    assert observation.reached() is SearchStage.NOT_ANSWERED
    rendered = observation.render()
    assert "1つも聴けませんでした" in rendered
    assert "貼ってください" not in rendered


def test_a_listener_that_never_attached_says_so_instead_of_blaming_the_platform() -> None:
    observation = SearchObservation(
        requested_url="https://example.test/searches",
        listen_path="/api/customers/members/search",
        listener_attached=False,
    )
    assert "聴く仕掛けが張れていません" in observation.render()


def test_a_body_the_platform_rejected_is_not_offered_as_a_template() -> None:
    """**画面自身の要求が通らないなら、写しても通らない。**"""
    observation = SearchObservation(
        requested_url="https://example.test/searches",
        listen_path="/api/customers/members/search",
        posts=(CapturedPost(status=500, body=BODY),),
        listener_attached=True,
    )
    assert observation.reached() is SearchStage.ALL_REJECTED
    assert "受け付けた POST が1つもありませんでした" in observation.render()


def test_an_accepted_body_that_is_not_json_is_reported_rather_than_guessed() -> None:
    observation = SearchObservation(
        requested_url="https://example.test/searches",
        listen_path="/api/customers/members/search",
        posts=(CapturedPost(status=200, body="not json"),),
        captured=None,
        listener_attached=True,
    )
    assert observation.reached() is SearchStage.UNREADABLE
    assert "推測で雛形を作ることはしません" in observation.render()


def test_a_successful_run_prints_the_template_and_the_condition_number() -> None:
    rendered = _found().render()
    assert _found().reached() is SearchStage.FOUND
    assert "739599" in rendered
    assert "{{PAGE}}" in rendered
    assert "送信も1件もしていません" in rendered


def test_the_blocked_third_party_count_is_printed_even_when_zero() -> None:
    """0件でも書く。黙ると「観測しなかった」と区別が付かない (原則2)。"""
    assert "止めた通信 (他所のオリジンへ): 0 件" in _found().render()


def test_a_state_that_contradicts_the_timeline_raises_instead_of_reporting() -> None:
    """本文が取れているのに POST は0件、という報告は嘘なので出さない。"""
    observation = SearchObservation(
        requested_url="https://example.test/searches",
        listen_path="/x",
        posts=(),
        captured=as_template(BODY),
    )
    with pytest.raises(ValueError, match="時系列と矛盾"):
        observation.reached()


def test_the_first_accepted_post_is_the_one_taken() -> None:
    listener = _SearchListener(path="/x")
    listener.posts = [
        CapturedPost(status=422, body='{"a": 1}'),
        CapturedPost(status=200, body='{"b": 2}'),
        CapturedPost(status=200, body='{"c": 3}'),
    ]
    accepted = listener.first_accepted()
    assert accepted is not None
    assert accepted.body == '{"b": 2}'


def test_no_accepted_post_yields_nothing_rather_than_the_rejected_one() -> None:
    listener = _SearchListener(path="/x")
    listener.posts = [CapturedPost(status=500, body='{"a": 1}')]
    assert listener.first_accepted() is None
