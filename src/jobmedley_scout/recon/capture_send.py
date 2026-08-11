"""Stage-3 reconnaissance: capture the send request without sending.

3章 段階3:

> **副作用のある操作は、受動的な観測では絶対に取れません。** 送信APIは送信ボタンを
> 押すまで発火せず、押せば本当に送信されてしまいます。

手順 (この順序が仕様):

1. 送信画面まで自動で到達する
2. **送信ボタンを押す直前に** ネットワーク層のブロックを武装し、以降の送信系POSTを
   記録してから中断 (abort) する
3. 武装前は素通し、GETリクエストは常に素通しする
4. 件名・本文に検知用の目印文字列を入れる
5. 武装ロジックはブラウザ非依存の形に切り出し、単体テストで固定する
   (:mod:`recon.gate` -- 「武装前は通す/武装後は止める/GETは常に通す」)

**このコマンドは常設する。動作確認後も削除しないこと** (3章)。媒体の画面変更の
たびに必要になる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jobmedley_scout.browser.navigation import goto, marker_present
from jobmedley_scout.browser.waits import pause
from jobmedley_scout.config.schema import BrowserConfig, WaitsConfig
from jobmedley_scout.recon.gate import GateDecision, SendGate
from jobmedley_scout.recon.sentinel import (
    find_sentinel_requests,
    make_sentinel,
    sentinel_body,
    sentinel_subject,
)


@dataclass(frozen=True)
class CapturedSend:
    """One request that was recorded and aborted while the gate was armed."""

    method: str
    url: str
    headers: dict[str, str]
    body: str | None
    carried_sentinel: bool


@dataclass(frozen=True)
class ReconResult:
    sentinel: str
    captured: tuple[CapturedSend, ...]
    reached_send_screen: bool
    note: str

    def likely_send_requests(self) -> tuple[CapturedSend, ...]:
        """Requests carrying our sentinel -- almost certainly the send call."""
        return tuple(entry for entry in self.captured if entry.carried_sentinel)


def install_gate(page: Any, gate: SendGate) -> None:
    """Route every request through the gate.

    ``page.route("**/*")`` は全リクエストを通す。判定そのものは :class:`SendGate`
    (ブラウザ非依存・単体テスト済み) が行い、ここはその判定を実行に移すだけ。
    ブラウザ依存部を薄く保つ設計 (13.4)。
    """

    def _handler(route: Any, request: Any) -> None:
        body: str | None
        try:
            body = request.post_data
        except Exception:
            body = None
        decision = gate.decide(request.method, request.url, body, dict(request.headers))
        if decision is GateDecision.RECORD_AND_ABORT:
            route.abort()
        else:
            route.continue_()

    page.route("**/*", _handler)


def capture_send_request(
    page: Any,
    *,
    run_id: str,
    list_url: str,
    list_ready_selector: str,
    open_composer: Any,
    fill_message: Any,
    click_send: Any,
    config: BrowserConfig,
    waits: WaitsConfig,
) -> ReconResult:
    """Drive to the send screen, arm, click send, and report what was aborted.

    ``open_composer`` / ``fill_message`` / ``click_send`` は呼び出し側が渡す
    コールバック。これらの操作に必要なセレクタは未確定の座標なので、偵察コマンドが
    対話的に与えるか、確定済みのものを渡す。**この関数自体は座標を推測しない。**
    """
    sentinel = make_sentinel(run_id)
    gate = SendGate()
    install_gate(page, gate)

    goto(page, list_url, config)
    if not marker_present(page, list_ready_selector, timeout_ms=config.selector_timeout_ms):
        return ReconResult(
            sentinel=sentinel,
            captured=(),
            reached_send_screen=False,
            note=(
                "候補者一覧に到達できませんでした。座標 nav.candidate_list_url / "
                "nav.list_ready_selector を確認してください。"
            ),
        )

    pause(waits.between_actions)
    open_composer(page)
    pause(waits.between_actions)
    fill_message(page, sentinel_subject(sentinel), sentinel_body(sentinel))
    pause(waits.between_actions)

    # ここまでは素通し。**送信ボタンを押す直前に武装する。**
    gate.arm()
    try:
        click_send(page)
        # 中断されたリクエストが記録されるまで少し待つ。
        pause(waits.between_actions)
    finally:
        # 武装窓はミリ秒単位に保つ。finally で必ず解除する (3章)。
        gate.disarm()

    recorded = tuple(
        CapturedSend(
            method=entry.method,
            url=entry.url,
            headers=dict(entry.headers),
            body=entry.body,
            carried_sentinel=bool(entry.body and sentinel in entry.body),
        )
        for entry in gate.recorded
    )
    sentinel_hits = find_sentinel_requests(
        tuple((entry.method, entry.url, entry.body) for entry in gate.recorded), sentinel
    )
    return ReconResult(
        sentinel=sentinel,
        captured=recorded,
        reached_send_screen=True,
        note=(
            f"武装中に {len(recorded)} 件の非GETリクエストを記録・中断しました "
            f"(うちセンチネルを含むもの {len(sentinel_hits)} 件)。"
            "無関係な計測ビーコンも記録されるのが正常です -- "
            "武装中は fail-closed で全ての非GETを止めるため (段階3では送信URL自体が未知)。"
        ),
    )
