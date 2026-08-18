"""判定を実行に移す1本の橋を固定する。

:class:`SendGate` がいくら正しく「止める」と言っても、その判定をブラウザへ渡す
``install_gate`` が ``route.continue_()`` を呼べば **リクエストは媒体のサーバへ
届く**。判定と実行の間のこの数行が、送信を止める最後の場所である。

ここで守るのは一点:

**「止める」判定が、どの形であれ ``continue_`` にならないこと。**
"""

from __future__ import annotations

from typing import Any

from jobmedley_scout.recon.capture_send import install_gate
from jobmedley_scout.recon.gate import GateMode, SendGate


class _Route:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.fulfilled: dict[str, Any] | None = None

    def abort(self) -> None:
        self.actions.append("abort")

    def fulfill(self, **kwargs: Any) -> None:
        self.actions.append("fulfill")
        self.fulfilled = kwargs

    def continue_(self) -> None:
        self.actions.append("continue")


class _Request:
    def __init__(self, method: str, url: str, body: str | None) -> None:
        self.method = method
        self.url = url
        self.post_data = body
        self.headers: dict[str, str] = {}


class _Page:
    def __init__(self) -> None:
        self.handler: Any = None

    def route(self, pattern: str, handler: Any) -> None:
        self.handler = handler


def _run(gate: SendGate, method: str, url: str, body: str | None = None) -> _Route:
    page = _Page()
    install_gate(page, gate)
    route = _Route()
    page.handler(route, _Request(method, url, body))
    return route


GRAPHQL_URL = "https://customers.job-medley.com/api/customers/graphql/MemberOnScoutProfile"
SEND_URL = "https://customers.job-medley.com/api/customers/scouts"


def test_a_get_is_let_through() -> None:
    gate = SendGate()
    gate.arm()
    assert _run(gate, "GET", SEND_URL).actions == ["continue"]


def test_a_blocked_write_is_aborted_not_continued() -> None:
    gate = SendGate()
    gate.arm()
    assert _run(gate, "POST", SEND_URL, "{}").actions == ["abort"]


def test_a_stubbed_write_is_fulfilled_not_continued() -> None:
    """**空の応答を返すのであって、通すのではない。**

    ここを ``continue_`` と取り違えると、緩和モードの実行で送信が飛ぶ。
    """
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    gate.arm()
    route = _run(gate, "POST", SEND_URL, "{}")

    assert route.actions == ["fulfill"]
    assert "continue" not in route.actions
    assert route.fulfilled is not None
    assert route.fulfilled["status"] == 200


def test_a_graphql_mutation_is_never_continued_even_in_the_relaxed_mode() -> None:
    """**スカウト送信はここに来る。**"""
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    gate.arm()
    body = '{"query": "mutation SendScout { sendScout { id } }"}'
    assert _run(gate, "POST", GRAPHQL_URL, body).actions == ["fulfill"]


def test_a_graphql_read_is_continued_in_the_relaxed_mode() -> None:
    """読み取りは本物の応答が要る。空の応答では画面が開かない。"""
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    gate.arm()
    body = '{"query": "query Profile { member { id } }"}'
    assert _run(gate, "POST", GRAPHQL_URL, body).actions == ["continue"]


def test_a_request_whose_body_cannot_be_read_is_still_blocked() -> None:
    """本文が読めない = 判定できない。**通さない。**"""

    class _Hostile:
        method = "POST"
        url = GRAPHQL_URL
        headers: dict[str, str] = {}

        @property
        def post_data(self) -> str:
            raise RuntimeError("post_data unavailable")

    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    gate.arm()
    page = _Page()
    install_gate(page, gate)
    route = _Route()
    page.handler(route, _Hostile())

    assert route.actions == ["fulfill"]
    assert "continue" not in route.actions
