"""``GateMode.BLOCK_SEND`` -- **押せる遮断。だから許可制でなければならない。**

``BLOCK_THIRD_PARTY`` は媒体のオリジンを丸ごと素通しにするので、押せば送信が
成立した。守っていたのは遮断ではなく「押さないこと」だけだった。

だがレジュメは **カードを押さないと飛ばない**。押さない偵察では原理的に観測
できない。そこで通す条件を許可制にする::

    GraphQL       読み取り (query) だけ通す。mutation は止める
    REST の POST  観測済みの読み取り経路だけ通す (search 系 / label)
    それ以外      止める

**送信は mutation である** (実測: ``graphql/SendSingleScout``)。
"""

from __future__ import annotations

import json

import pytest

from jobmedley_scout.recon.gate import (
    READ_PATH_SEGMENTS,
    GateDecision,
    GateMode,
    SendGate,
    is_read_path,
)

MEDIA = "https://customers.job-medley.com"

#: 実測した送信。**このモードで最も止まっていなければならないもの。**
SEND_URL = f"{MEDIA}/api/customers/graphql/SendSingleScout"
SEND_BODY = json.dumps(
    {
        "operationName": "SendSingleScout",
        "query": (
            "mutation SendSingleScout($input: SendSingleScoutInput!)"
            " { sendSingleScout(input: $input) { id } }"
        ),
        "variables": {"input": {"memberId": "1", "scoutMessage": "x"}},
    }
)

#: 実測したレジュメ。**通っていなければ観測が成立しない。**
RESUME_URL = f"{MEDIA}/api/customers/graphql/MemberOnScoutProfileModalOfDesktop"
RESUME_BODY = json.dumps(
    {
        "operationName": "MemberOnScoutProfileModalOfDesktop",
        "query": "query MemberOnScoutProfileModalOfDesktop($id: ID!) { member(id: $id) { id } }",
        "variables": {"id": "1"},
    }
)


def _armed() -> SendGate:
    gate = SendGate(mode=GateMode.BLOCK_SEND)
    gate.arm()
    return gate


def test_the_send_mutation_is_blocked() -> None:
    """**これが1行で崩れると、取り消せない送信が起きる** (13.6)。"""
    gate = _armed()
    try:
        decision = gate.decide("POST", SEND_URL, SEND_BODY)
    finally:
        gate.disarm()
    assert decision is not GateDecision.PASS
    assert gate.recorded, "止めたのに記録が残っていません"
    assert not gate.passed_reads, "止めたものが『通した』側へ記録されています"


def test_the_profile_query_is_passed() -> None:
    """レジュメは GraphQL の **query** なので通る。通らなければ観測できない。"""
    gate = _armed()
    try:
        assert gate.decide("POST", RESUME_URL, RESUME_BODY) is GateDecision.PASS
    finally:
        gate.disarm()


def test_the_candidate_list_rest_post_is_passed() -> None:
    """**一覧は REST の POST である** (実測)。

    ``BLOCK_WRITES`` はこれを止めていたので行が消え、押す対象が無くなった
    (実測22回目)。押すコマンドでは致命的である。
    """
    gate = _armed()
    try:
        decision = gate.decide("POST", f"{MEDIA}/api/customers/members/search/", "{}")
    finally:
        gate.disarm()
    assert decision is GateDecision.PASS


@pytest.mark.parametrize(
    "path",
    [
        "/api/customers/customer_search_conditions/search_manual/",
        "/api/customers/customer_search_conditions/search_recommend/",
        "/api/customers/received_favorites/search/",
        "/api/customers/scouted_members/search/",
        "/api/customers/customer_search_conditions/label/",
        "/api/customers/members/search/",
    ],
)
def test_every_observed_read_path_is_passed(path: str) -> None:
    """実測23/24回目に一覧を開いて飛んだ6本。**全部通ること。**"""
    gate = _armed()
    try:
        assert gate.decide("POST", MEDIA + path, "{}") is GateDecision.PASS
    finally:
        gate.disarm()


@pytest.mark.parametrize(
    "path",
    [
        "/api/customers/favorites/",
        "/api/customers/members/1/scout/",
        "/api/customers/messages/send/",
        "/api/customers/customer_users/logout/",
        "/api/customers/searches/",
    ],
)
def test_an_unobserved_rest_post_is_blocked(path: str) -> None:
    """**許可制。知らないものは全部止まる。**

    拒否制 (「送信のURLだけ止める」) にすると、知らない送信路に対して素通しに
    なる。許可制ならそこが閉じる。
    """
    gate = _armed()
    try:
        decision = gate.decide("POST", MEDIA + path, "{}")
    finally:
        gate.disarm()
    assert decision is not GateDecision.PASS, f"{path} が素通りしています"


def test_third_parties_are_still_blocked() -> None:
    gate = _armed()
    try:
        beacon = gate.decide(
            "POST",
            "https://www.google-analytics.com/g/collect?dl=https%3A%2F%2Fcustomers.job-medley.com%2F",
            "{}",
        )
        # 他所のオリジンの ``/search/`` も、許可経路の名前を借りているだけ。
        borrowed = gate.decide("POST", "https://evil.example/api/customers/members/search/", "{}")
    finally:
        gate.disarm()
    assert beacon is not GateDecision.PASS
    assert borrowed is not GateDecision.PASS, "他所のオリジンが許可経路を名乗って通っています"


def test_gets_still_pass() -> None:
    gate = _armed()
    try:
        assert gate.decide("GET", f"{MEDIA}/customers/searches?lg=0") is GateDecision.PASS
    finally:
        gate.disarm()


def test_a_graphql_batch_with_one_mutation_is_blocked() -> None:
    """まとめ送りに mutation が1つでも混ざれば全体を止める。"""
    batch = json.dumps(
        [
            {"query": "query A { a }"},
            {"query": "mutation B { b }"},
        ]
    )
    gate = _armed()
    try:
        decision = gate.decide("POST", f"{MEDIA}/api/customers/graphql/Batch", batch)
    finally:
        gate.disarm()
    assert decision is not GateDecision.PASS


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{MEDIA}/api/customers/members/search/", True),
        (f"{MEDIA}/api/customers/members/search", True),
        (f"{MEDIA}/api/customers/customer_search_conditions/label/", True),
        (f"{MEDIA}/api/customers/members/search/extra/", False),
        (f"{MEDIA}/", False),
        ("", False),
    ],
)
def test_is_read_path_only_matches_the_last_segment(url: str, expected: bool) -> None:
    """**末尾の節だけを見る。** 部分一致にすると ``/search/send/`` まで通る。"""
    assert is_read_path(url) is expected


def test_the_allowlist_names_only_read_endpoints() -> None:
    """**ここに書き込み経路を1つ入れれば、このモードの意味が消える。**

    増やすときは「名前がそれらしいから」ではなく、押さない偵察で実際に飛んだ
    ことを根拠にすること (原則3)。
    """
    assert READ_PATH_SEGMENTS == frozenset({"search", "search_manual", "search_recommend", "label"})


def test_the_default_mode_is_still_the_strict_one() -> None:
    assert SendGate().mode is GateMode.BLOCK_ALL


# ===========================================================================
# 知っていて受け入れる書き込み (実測25回目)
# ===========================================================================

MARK_READ_URL = f"{MEDIA}/api/customers/members/mark_read/"


def test_a_write_is_blocked_unless_the_caller_names_it() -> None:
    """**既定は空。** 受け入れるかどうかは呼び出し側が名前で明示する。

    実測25回目、遮断は ``members/mark_read`` を止めた。許可制が予期していなかった
    書き込みを実際に捕まえたということで、**これが既定の正しい振る舞いである**。
    """
    gate = _armed()
    try:
        decision = gate.decide("POST", MARK_READ_URL, '{"member_ids": [1]}')
    finally:
        gate.disarm()
    assert decision is not GateDecision.PASS
    assert not gate.accepted_passed


def test_a_named_write_passes_and_is_counted_separately() -> None:
    """**読み取りに混ぜない。** 何を書いたかが報告から消えると意味が無い。"""
    from jobmedley_scout.recon.gate import KNOWN_WRITE_MARK_READ

    gate = SendGate(mode=GateMode.BLOCK_SEND, accepted_writes=frozenset({KNOWN_WRITE_MARK_READ}))
    gate.arm()
    try:
        decision = gate.decide("POST", MARK_READ_URL, '{"member_ids": [1]}')
    finally:
        gate.disarm()
    assert decision is GateDecision.PASS
    assert len(gate.accepted_passed) == 1
    assert gate.accepted_passed[0].body is None, "書き込みの本文を残しています (13.2)"


def test_accepting_one_write_does_not_accept_the_send() -> None:
    """**ここが崩れたら全部が無意味になる。**

    受け入れ集合は経路の末尾で照合する。送信は GraphQL の mutation なので、
    どの書き込みを受け入れても通ってはいけない。
    """
    from jobmedley_scout.recon.gate import KNOWN_WRITE_MARK_READ

    gate = SendGate(mode=GateMode.BLOCK_SEND, accepted_writes=frozenset({KNOWN_WRITE_MARK_READ}))
    gate.arm()
    try:
        decision = gate.decide("POST", SEND_URL, SEND_BODY)
    finally:
        gate.disarm()
    assert decision is not GateDecision.PASS


def test_accepting_a_write_does_not_open_other_writes() -> None:
    from jobmedley_scout.recon.gate import KNOWN_WRITE_MARK_READ

    gate = SendGate(mode=GateMode.BLOCK_SEND, accepted_writes=frozenset({KNOWN_WRITE_MARK_READ}))
    gate.arm()
    try:
        other = gate.decide("POST", f"{MEDIA}/api/customers/favorites/", "{}")
    finally:
        gate.disarm()
    assert other is not GateDecision.PASS


def test_an_accepted_write_on_another_origin_is_still_blocked() -> None:
    from jobmedley_scout.recon.gate import KNOWN_WRITE_MARK_READ

    gate = SendGate(mode=GateMode.BLOCK_SEND, accepted_writes=frozenset({KNOWN_WRITE_MARK_READ}))
    gate.arm()
    try:
        borrowed = gate.decide("POST", "https://evil.example/api/members/mark_read/", "{}")
    finally:
        gate.disarm()
    assert borrowed is not GateDecision.PASS


def test_accepted_writes_are_kept_out_of_the_read_allowlist() -> None:
    """**分けてあることそのものを固定する。**

    ``READ_PATH_SEGMENTS`` は「副作用が無いと観測できたもの」、受け入れ集合は
    「副作用が在ると分かっていて、それでも通すもの」である。混ぜれば、受け入れた
    覚えのない書き込みが読み取りの顔をして増えていく。
    """
    from jobmedley_scout.recon.gate import KNOWN_WRITE_MARK_READ

    assert KNOWN_WRITE_MARK_READ not in READ_PATH_SEGMENTS
