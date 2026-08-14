"""段階3の探索の **安全不変条件** を固定する。

このコマンドは、押せば本当にスカウトが飛ぶボタンを押す。安全はただ1点で担保
されている: **押す前に遮断が武装されていること。** 順序が逆になれば取り消せない
送信が起きる。だからここでは、機能ではなく順序と遮断の実効性を検査する。

1. どのクリックの時点でも遮断は武装済みである
2. 遮断は非GETを1件も通さない (fail-closed)
3. 武装できなかった実行は、ボタンを1つも押さず、報告でもそう述べる
4. 押したら閉じたことを **確認できた** ものだけを座標の値にする (原則3)
"""

from __future__ import annotations

from jobmedley_scout.browser.dom import DomNode, DomTree
from jobmedley_scout.recon.capture_open import (
    AttemptResult,
    OpenObservation,
    explore_card_actions,
)
from jobmedley_scout.recon.gate import GateDecision, SendGate
from jobmedley_scout.recon.open_structure import BlockedRequest

URL = "https://customers.job-medley.com/customers/searches?age[from]=0&age[to]=40"


def _tree(*rows: tuple[str, tuple[str, ...], int]) -> DomTree:
    return DomTree(
        nodes=tuple(DomNode(tag=t, class_names=c, parent=p) for t, c, p in rows),
        truncated=False,
        shadow_root_count=0,
    )


def _card_page_tree() -> DomTree:
    return _tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("c-search-member-card",), 0),  # 1
        ("div", ("c-search-member-card__buttons",), 1),  # 2
        ("button", ("c-button", "u-wd-100p"), 2),  # 3  用途不明
        ("button", ("c-button", "js-tour-guide-scout-button"), 2),  # 4  スカウト送信
    )


class _FakeLocatorHandle:
    def __init__(self, page: _FakePage, selector: str, index: int) -> None:
        self._page = page
        self._selector = selector
        self._index = index

    def click(self, timeout: int = 0) -> None:
        self._page.clicks.append((self._selector, self._index, self._page.gate.is_armed))
        # 押された結果として、このページは「ドロワーが開いた」ことにする。
        self._page.opened = True
        # 送信ボタンなら、媒体は送信POSTを投げようとする -- 遮断の出番。
        if "scout" in self._selector:
            self._page.fire_request("POST", "https://customers.job-medley.com/api/scouts", "SENT-1")

    def is_visible(self) -> bool:
        return True

    def count(self) -> int:
        return 1

    @property
    def first(self) -> _FakeLocatorHandle:
        return self


class _FakeLocator:
    def __init__(self, page: _FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector

    def count(self) -> int:
        return 2 if self._selector.startswith("button") else 0

    def nth(self, index: int) -> _FakeLocatorHandle:
        return _FakeLocatorHandle(self._page, self._selector, index)

    @property
    def first(self) -> _FakeLocatorHandle:
        return _FakeLocatorHandle(self._page, self._selector, 0)


class _FakePage:
    """遮断が本当に効くかを見るための最小のページ。

    ``fire_request`` は媒体が通信を投げた瞬間の再現である。gate が
    ``RECORD_AND_ABORT`` を返せば **送信は起きない** -- ``sent`` に積まれない。
    """

    url = URL

    def __init__(self, gate: SendGate, tree: DomTree) -> None:
        self.gate = gate
        self._tree = tree
        self.clicks: list[tuple[str, int, bool]] = []
        self.sent: list[str] = []
        self.opened = False

    # --- ブラウザ API の最小再現 ---
    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    def wait_for_function(self, script: str, arg: object = None, timeout: int = 0) -> None:
        return None

    def keyboard_press(self, key: str) -> None:  # pragma: no cover - 呼ばれない経路
        return None

    @property
    def keyboard(self) -> _FakePage:
        return self

    def press(self, key: str) -> None:
        return None

    def evaluate(self, script: str, arg: object = None) -> object:
        # dom_tree はこの経路では使わない (テストは monkeypatch で木を差し込む)。
        return None

    # --- 通信の再現 ---
    def fire_request(self, method: str, url: str, body: str) -> None:
        decision = self.gate.decide(method, url, body, {})
        if decision is GateDecision.PASS:
            self.sent.append(url)  # **これが起きたら送信されたということ**


def _install(monkeypatch, page: _FakePage, tree: DomTree) -> None:
    """ブラウザ依存の読み取りを、この木を返すだけの関数に置き換える。"""
    import jobmedley_scout.recon.capture_open as module

    monkeypatch.setattr(module, "dom_tree", lambda _page: tree)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_interactive", lambda *a, **k: None)
    monkeypatch.setattr(module, "goto", lambda *a, **k: None)
    monkeypatch.setattr(module, "_dismiss_tour", lambda *a, **k: None)
    monkeypatch.setattr(module, "_close_landing_modals", lambda *a, **k: None)


class _Config:
    selector_timeout_ms = 100


def test_every_click_happens_while_the_gate_is_armed(monkeypatch) -> None:
    """**このテストが落ちたら送信事故が起きうる。**

    探索は押す前に武装されている前提で書かれている。各クリックの瞬間の
    ``is_armed`` を記録し、1つでも False が無いことを固定する。
    """
    tree = _card_page_tree()
    gate = SendGate()
    page = _FakePage(gate, tree)
    _install(monkeypatch, page, tree)

    gate.arm()
    try:
        explore_card_actions(
            page,
            tree=tree,
            row_index=1,
            sentinel="SENT-1",
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
        )
    finally:
        gate.disarm()

    assert page.clicks, "1つも押していないならテストの意味が無い"
    assert all(armed for _selector, _nth, armed in page.clicks)


def test_pressing_the_scout_button_sends_nothing(monkeypatch) -> None:
    """スカウトボタンを押しても送信は起きない -- 遮断が非GETを止めるため。

    これが成り立つから、このコマンドは「押してよいか分からないボタン」を押せる。
    """
    tree = _card_page_tree()
    gate = SendGate()
    page = _FakePage(gate, tree)
    _install(monkeypatch, page, tree)

    gate.arm()
    try:
        results = explore_card_actions(
            page,
            tree=tree,
            row_index=1,
            sentinel="SENT-1",
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
        )
    finally:
        gate.disarm()

    assert page.sent == []  # **送信は1件も起きていない**
    pressed = [r.selector for r in results]
    assert any("scout" in s for s in pressed)  # それでもスカウトボタンは押している
    # 遮断した通信が観測として返っている (= 送信路の正体が分かる)。
    blocked = [entry for r in results for entry in r.blocked]
    assert any(entry.carried_sentinel for entry in blocked)


def test_the_scout_button_is_pressed_last(monkeypatch) -> None:
    """安全そうな方を先に押す。先にドロワーが開けば送信部品に触れずに済む。"""
    tree = _card_page_tree()
    gate = SendGate()
    page = _FakePage(gate, tree)
    _install(monkeypatch, page, tree)

    gate.arm()
    try:
        results = explore_card_actions(
            page,
            tree=tree,
            row_index=1,
            sentinel="S",
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
        )
    finally:
        gate.disarm()

    assert results[-1].looks_like_send is True
    assert all(not r.looks_like_send for r in results[:-1])


# --- 報告の側 -----------------------------------------------------------------


def test_a_run_that_could_not_arm_says_it_pressed_nothing() -> None:
    """武装できなかった実行は「値なし」ではなく **「押していない」** と述べる。

    「値が出なかった」と「そもそも試していない」は別の事実である (原則3)。
    """
    report = OpenObservation(requested_url=URL, gate_armed=False, note="武装の確認に失敗").render()

    assert "ボタンを1つも押していません" in report
    assert "武装の確認に失敗" in report


def test_only_close_controls_whose_effect_was_observed_become_the_value() -> None:
    """開いた領域の中に在っただけでは値にしない。**押したら消えた** ものだけ。"""
    verified = AttemptResult(
        selector="button.c-button",
        nth=0,
        looks_like_send=False,
        clicked=True,
        gained=("div.c-side-cover__body",),
        close_candidates=("a.c-side-cover__close-btn",),
        close_verified=True,
    )
    unverified = AttemptResult(
        selector="button.c-other",
        nth=1,
        looks_like_send=False,
        clicked=True,
        gained=("div.c-modal__body",),
        close_candidates=("a.c-modal__closer",),
        close_verified=False,
    )
    observed = OpenObservation(
        requested_url=URL, gate_armed=True, rows_found=True, attempts=(verified, unverified)
    )

    assert observed.confirmed_close_selectors() == ("a.c-side-cover__close-btn",)
    report = observed.render()
    assert 'nav.drawer_close_selectors: ["a.c-side-cover__close-btn"]' in report
    assert "a.c-modal__closer" not in report.split("config/site_coordinates.yaml")[1]


def test_a_drawer_that_never_opened_is_reported_as_unresolved() -> None:
    attempt = AttemptResult(
        selector="button.c-button", nth=0, looks_like_send=False, clicked=True, gained=()
    )
    report = OpenObservation(
        requested_url=URL, gate_armed=True, rows_found=True, attempts=(attempt,)
    ).render()

    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "どのボタンでもドロワーは開きませんでした" in report


def test_the_send_url_comes_from_the_request_that_carried_the_sentinel() -> None:
    """目印を運んだ非GETだけが送信路を名乗れる。**中断済みである旨も述べる。**"""
    attempt = AttemptResult(
        selector="button.js-tour-guide-scout-button",
        nth=1,
        looks_like_send=True,
        clicked=True,
        blocked=(
            BlockedRequest("POST", "https://analytics.example/collect", carried_sentinel=False),
            BlockedRequest(
                "POST", "https://customers.job-medley.com/api/scouts", carried_sentinel=True
            ),
        ),
    )
    report = OpenObservation(
        requested_url=URL, gate_armed=True, rows_found=True, attempts=(attempt,)
    ).render()

    assert 'api.send.paid.url_pattern: "https://customers.job-medley.com/api/scouts"' in report
    assert "送信は行われていません" in report


def test_printed_urls_hide_member_ids() -> None:
    """報告に出るURLから会員IDを消す (13.2)。原文は構造ダンプにだけ残る。"""
    attempt = AttemptResult(
        selector="button.x",
        nth=0,
        looks_like_send=True,
        clicked=True,
        blocked=(
            BlockedRequest(
                "POST",
                "https://customers.job-medley.com/customers/members/48211/scouts",
                carried_sentinel=False,
            ),
        ),
    )
    report = OpenObservation(
        requested_url=URL, gate_armed=True, rows_found=True, attempts=(attempt,)
    ).render()

    assert "48211" not in report
    assert "/customers/members/{id}/scouts" in report
