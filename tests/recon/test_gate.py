"""3章 段階3: 「武装前は通す / 武装後は止める / GETは常に通す」を固定する。

ブラウザ実装はテストできないので、**判定はここで固定されている**必要がある
(13.4)。このファイルが落ちるときは、偵察が実送信を出しうる状態になっている。
"""

from __future__ import annotations

import json

from jobmedley_scout.recon.gate import SAFE_METHODS, GateDecision, GateMode, SendGate

SEND_URL = "https://example.invalid/api/v2/scout/messages"
TELEMETRY_URL = "https://beacon.example.invalid/collect"


def test_before_arming_everything_passes() -> None:
    """武装前は通す。送信画面まで普通に到達できなければ偵察が成立しない。"""
    gate = SendGate()

    assert gate.decide("POST", SEND_URL, '{"body": "x"}') is GateDecision.PASS
    assert gate.decide("GET", SEND_URL) is GateDecision.PASS
    assert gate.is_armed is False
    assert gate.recorded == ()


def test_while_armed_every_non_get_is_blocked() -> None:
    """武装後は止める。**URLもセンチネルも見ない** のが要点。

    段階3では送信URLそのものが未知なので、URL一致で判定するのは循環参照になる。
    センチネル一致で判定すると、payload にセンチネルが載らない送信が素通しになる。
    """
    gate = SendGate()
    gate.arm()

    assert gate.decide("POST", SEND_URL, "ZZRECON-XXXX") is GateDecision.RECORD_AND_ABORT
    # センチネルを含まない、送信APIらしくもないPOSTも同じく止まる。
    assert gate.decide("POST", TELEMETRY_URL, "{}") is GateDecision.RECORD_AND_ABORT

    assert len(gate.recorded) == 2


def test_get_always_passes() -> None:
    """GETは常に通す -- 武装中でも。画面の描画に必要で、副作用が無い。"""
    gate = SendGate()

    assert gate.decide("GET", SEND_URL) is GateDecision.PASS
    gate.arm()
    assert gate.decide("GET", SEND_URL) is GateDecision.PASS
    assert gate.decide("HEAD", SEND_URL) is GateDecision.PASS
    assert gate.recorded == ()


def test_recorded_requests_preserve_order_and_body() -> None:
    """記録の順序と本文は解析の material。連番と本文をそのまま残す。"""
    gate = SendGate()
    gate.arm()

    gate.decide("POST", TELEMETRY_URL, "first", {"content-type": "text/plain"})
    gate.decide("POST", SEND_URL, "second", {"content-type": "application/json"})
    gate.decide("POST", SEND_URL, None)

    recorded = gate.recorded
    assert [entry.body for entry in recorded] == ["first", "second", None]
    assert [entry.sequence for entry in recorded] == [1, 2, 3]
    assert [entry.url for entry in recorded] == [TELEMETRY_URL, SEND_URL, SEND_URL]
    assert recorded[1].headers == {"content-type": "application/json"}
    assert recorded[2].headers == {}


def test_recorded_headers_are_copied_not_aliased() -> None:
    """呼び出し側が使い回すヘッダ辞書で、記録済みの証拠が書き換わってはならない。"""
    gate = SendGate()
    gate.arm()
    headers = {"x-token": "abc"}

    gate.decide("POST", SEND_URL, "b", headers)
    headers["x-token"] = "MUTATED"

    assert gate.recorded[0].headers == {"x-token": "abc"}


def test_clear_empties_recorded() -> None:
    gate = SendGate()
    gate.arm()
    gate.decide("POST", SEND_URL, "b")
    assert len(gate.recorded) == 1

    gate.clear()

    assert gate.recorded == ()


def test_sequence_does_not_restart_after_clear() -> None:
    """連番を戻すと、消す前と後の「1番」が解析ログ上で区別できなくなる。"""
    gate = SendGate()
    gate.arm()
    gate.decide("POST", SEND_URL, "b")
    gate.clear()

    gate.decide("POST", SEND_URL, "b")

    assert gate.recorded[0].sequence == 2


def test_disarm_restores_pass_through() -> None:
    """``disarm()`` は ``finally`` に置く。解除後は元の素通しに戻る。"""
    gate = SendGate()
    gate.arm()
    assert gate.decide("POST", SEND_URL, "b") is GateDecision.RECORD_AND_ABORT

    gate.disarm()

    assert gate.is_armed is False
    assert gate.decide("POST", SEND_URL, "b") is GateDecision.PASS
    # 解除しても、既に記録した証拠は残る。
    assert len(gate.recorded) == 1


def test_arm_does_not_wipe_previous_recording() -> None:
    """武装のたびに黙って証拠が消えると、複数回の試行を突き合わせられない。"""
    gate = SendGate()
    gate.arm()
    gate.decide("POST", SEND_URL, "b")
    gate.disarm()

    gate.arm()

    assert len(gate.recorded) == 1


def test_put_patch_delete_are_blocked_when_armed() -> None:
    """POST 以外の更新系も止める。媒体が PUT で送信していない保証は無い。"""
    gate = SendGate()
    gate.arm()

    for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):
        assert gate.decide(method, SEND_URL, "b") is GateDecision.RECORD_AND_ABORT

    assert [entry.method for entry in gate.recorded] == ["PUT", "PATCH", "DELETE", "OPTIONS"]


def test_unknown_or_lowercased_methods_are_blocked_when_armed() -> None:
    """安全と確実に分かるものだけ通す。正規化の穴を fail-closed の穴にしない。"""
    gate = SendGate()
    gate.arm()

    assert gate.decide("get", SEND_URL) is GateDecision.RECORD_AND_ABORT
    assert gate.decide("", SEND_URL) is GateDecision.RECORD_AND_ABORT
    assert gate.decide("QUERY", SEND_URL) is GateDecision.RECORD_AND_ABORT


def test_safe_methods_never_grows_to_include_a_mutating_method() -> None:
    """SAFE_METHODS に POST が入った時点で、このモジュールの存在意義が消える。"""
    expected = frozenset({"GET", "HEAD"})
    assert SAFE_METHODS == expected


# --- 緩和モード (実測5回目: 媒体が GraphQL の単一ページアプリだった) -------------


GRAPHQL_URL = "https://customers.job-medley.com/api/customers/graphql/MemberOnScoutProfile"


def _query_body(document: str = "query A { a }") -> str:
    return json.dumps({"query": document})


def test_the_default_mode_still_blocks_every_non_get() -> None:
    """**既定は変わっていない。** 緩和は名前で明示したときだけ効く。

    このテストが落ちたら、既定の安全性が黙って緩んだということである。
    """
    gate = SendGate()
    gate.arm()
    assert gate.mode is GateMode.BLOCK_ALL
    assert gate.decide("POST", GRAPHQL_URL, _query_body()) is GateDecision.RECORD_AND_ABORT
    assert gate.passed_reads == ()


def test_block_writes_lets_a_graphql_read_through() -> None:
    """これを通さないと画面が開かず、段階3は原理的に終われない。"""
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    gate.arm()
    assert gate.decide("POST", GRAPHQL_URL, _query_body()) is GateDecision.PASS
    # **通した事実も観測として残る。**
    assert [entry.url for entry in gate.passed_reads] == [GRAPHQL_URL]
    assert gate.recorded == ()


def test_block_writes_never_lets_a_mutation_through() -> None:
    """**スカウト送信はここに来る。** 緩和しても、ここは絶対に通さない。"""
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    gate.arm()
    decision = gate.decide(
        "POST", GRAPHQL_URL, _query_body("mutation SendScout { sendScout { id } }")
    )
    assert decision is GateDecision.RECORD_AND_STUB
    assert gate.passed_reads == ()
    assert [entry.url for entry in gate.recorded] == [GRAPHQL_URL]


def test_block_writes_never_lets_a_plain_post_through() -> None:
    """GraphQL でない更新系は緩和の対象外である。"""
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    gate.arm()
    decision = gate.decide("POST", "https://customers.job-medley.com/api/customers/scouts", "{}")
    assert decision is GateDecision.RECORD_AND_STUB


def test_block_writes_blocks_with_a_stub_not_an_abort() -> None:
    """中断だと媒体が共通エラー処理で画面ごと飛ばす (実測5回目)。

    空の応答なら画面が残り、探索を続けられる。**サーバへ到達しない点は同じ。**
    """
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    gate.arm()
    assert gate.decide("PUT", "https://example.com/x") is GateDecision.RECORD_AND_STUB


def test_the_relaxation_does_nothing_before_arming() -> None:
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    assert gate.decide("POST", GRAPHQL_URL, _query_body()) is GateDecision.PASS
    assert gate.passed_reads == ()  # 武装していないので「通した」記録も無い


def test_a_passed_read_keeps_no_body() -> None:
    """通した読み取りの本文には画面の値が載りうる。**持たない** (13.2)。"""
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    gate.arm()
    gate.decide("POST", GRAPHQL_URL, _query_body())
    assert gate.passed_reads[0].body is None
