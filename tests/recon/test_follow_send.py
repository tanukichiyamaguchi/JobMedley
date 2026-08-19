"""教わった導線をそのまま辿る偵察。

**順序を間違えると1往復まるごと失う。** 実機で確かめられるのは GitHub Actions
から1回だけで、10分かかる。だから順序そのもの (フォームを開く → 求人を選ぶ →
本文に目印 → 前進する) は、ブラウザ抜きでここに固定する。

偽のページは運用者が示した画面をそのまま写している。押すたびに木が入れ替わり、
必須欄が埋まっていなければフォームは不備を訴える -- 実物と同じ振る舞いを
最小限で再現し、**埋めずに押せば通らない** ことまで含めて確かめる。
"""

from __future__ import annotations

from typing import Any

from jobmedley_scout.browser.dom import DomNode, DomTree
from jobmedley_scout.recon.follow_send import (
    SendWalk,
    SendWalkStage,
    Step,
    walk_form,
)
from jobmedley_scout.recon.gate import GateDecision, GateMode, SendGate
from jobmedley_scout.recon.sentinel import make_sentinel, sentinel_body

URL = "https://customers.job-medley.com/customers/searches"
SEND_URL = "https://customers.job-medley.com/api/customers/scouts/03323741"


def _tree(*rows: tuple[str, tuple[str, ...], int]) -> DomTree:
    return DomTree(
        nodes=tuple(DomNode(tag=t, class_names=c, parent=p) for t, c, p in rows),
        truncated=False,
        shadow_root_count=0,
    )


#: 一覧。カードに「プロフィール確認」と「スカウトを送る」が並ぶ。
LIST = _tree(
    ("body", ("c-body",), -1),  # 0
    ("div", ("c-search-member-card",), 0),  # 1
    ("div", ("c-search-member-card__buttons",), 1),  # 2
    ("button", ("c-button", "u-wd-100p"), 2),  # 3  プロフィール確認
    ("button", ("c-button", "u-wd-100p", "js-tour-guide-scout-button"), 2),  # 4  スカウトを送る
)

#: 送信フォーム (サイドカバー)。左に経歴、右に入力欄。
FORM = _tree(
    ("body", ("c-body", "c-body--fixed-by-sidecover"), -1),  # 0
    ("div", ("c-search-member-card",), 0),  # 1
    ("div", ("c-search-member-card__buttons",), 1),  # 2
    ("button", ("c-button", "u-wd-100p"), 2),  # 3
    ("button", ("c-button", "u-wd-100p", "js-tour-guide-scout-button"), 2),  # 4
    ("div", ("c-sidecover",), 0),  # 5
    ("div", ("c-scout-form",), 5),  # 6  ← form_root
    ("input", ("c-text-field",), 6),  # 7  スカウト対象求人
    ("textarea", ("c-textarea",), 6),  # 8  本文
    ("button", ("c-button", "c-button--important", "c-button--center"), 6),  # 9
)

#: 求人の欄を押すと候補が出る。
SUGGEST = _tree(
    ("body", ("c-body", "c-body--fixed-by-sidecover"), -1),  # 0
    ("div", ("c-search-member-card",), 0),  # 1
    ("div", ("c-search-member-card__buttons",), 1),  # 2
    ("button", ("c-button", "u-wd-100p"), 2),  # 3
    ("button", ("c-button", "u-wd-100p", "js-tour-guide-scout-button"), 2),  # 4
    ("div", ("c-sidecover",), 0),  # 5
    ("div", ("c-scout-form",), 5),  # 6
    ("input", ("c-text-field",), 6),  # 7
    ("textarea", ("c-textarea",), 6),  # 8
    ("button", ("c-button", "c-button--important", "c-button--center"), 6),  # 9
    ("ul", ("c-typeahead__list",), 6),  # 10
    ("li", ("c-typeahead__item",), 10),  # 11
    ("li", ("c-typeahead__item",), 10),  # 12
)

#: 確認の段。**本文欄は無い** -- 書いた内容を読ませる段だから。
CONFIRM = _tree(
    ("body", ("c-body", "c-body--fixed-by-sidecover"), -1),  # 0
    ("div", ("c-search-member-card",), 0),  # 1
    ("div", ("c-search-member-card__buttons",), 1),  # 2
    ("button", ("c-button", "u-wd-100p"), 2),  # 3
    ("button", ("c-button", "u-wd-100p", "js-tour-guide-scout-button"), 2),  # 4
    ("div", ("c-sidecover",), 0),  # 5
    ("div", ("c-scout-confirm",), 5),  # 6
    ("button", ("c-button", "c-button--important", "c-button--send"), 6),  # 7
)

#: 必須欄が空のまま押したときにフォームが返すもの。
REJECTED = _tree(
    ("body", ("c-body", "c-body--fixed-by-sidecover"), -1),  # 0
    ("div", ("c-search-member-card",), 0),  # 1
    ("div", ("c-search-member-card__buttons",), 1),  # 2
    ("button", ("c-button", "u-wd-100p"), 2),  # 3
    ("button", ("c-button", "u-wd-100p", "js-tour-guide-scout-button"), 2),  # 4
    ("div", ("c-sidecover",), 0),  # 5
    ("div", ("c-scout-form",), 5),  # 6
    ("input", ("c-text-field",), 6),  # 7
    ("textarea", ("c-textarea", "js-error"), 6),  # 8
    ("button", ("c-button", "c-button--important", "c-button--center"), 6),  # 9
    ("ul", ("js-validation-error-message",), 6),  # 10
    ("li", ("form-error",), 10),  # 11
)


class _Config:
    selector_timeout_ms = 100


class _Handle:
    def __init__(self, page: _Page, selector: str, index: int) -> None:
        self._page = page
        self._selector = selector
        self._index = index

    def click(self, timeout: int = 0) -> None:
        self._page.press(self._selector)

    def dispatch_event(self, name: str, timeout: int = 0) -> None:
        self._page.press(self._selector)

    def fill(self, text: str, timeout: int = 0) -> None:
        if not self._selector.startswith("textarea"):
            raise RuntimeError("Element is not an <input>, <textarea> or [contenteditable]")
        self._page.body = text

    def input_value(self) -> str:
        if self._selector.startswith("textarea"):
            return self._page.body
        if self._selector.startswith("input"):
            return self._page.offer
        raise RuntimeError("Element is not an <input>, <textarea> or [contenteditable]")


class _Locator:
    def __init__(self, page: _Page, selector: str) -> None:
        self._page = page
        self._selector = selector

    def count(self) -> int:
        return 1

    def nth(self, index: int) -> _Handle:
        return _Handle(self._page, self._selector, index)

    @property
    def first(self) -> _Handle:
        return _Handle(self._page, self._selector, 0)


class _Page:
    """運用者が示した画面を最小限で写したもの。

    **必須欄が空なら通さない。** 実物のフォームがそうだからで、この振る舞いが
    無いと「埋めずに押しても通ってしまう」偽物になり、順序の検証にならない。
    """

    url = URL

    def __init__(self, gate: SendGate) -> None:
        self.gate = gate
        self.tree = LIST
        self.offer = ""
        self.body = ""
        self.sent: list[str] = []
        self.pressed: list[tuple[str, bool]] = []

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    def fire_request(self, method: str, url: str, body: str) -> None:
        if self.gate.decide(method, url, body, {}) is GateDecision.PASS:
            self.sent.append(url)  # **これが起きたら送信されたということ**

    def press(self, selector: str) -> None:
        self.pressed.append((selector, self.gate.is_armed))
        if "js-tour-guide-scout-button" in selector:
            self.tree = FORM
        elif selector.startswith("input"):
            self.tree = SUGGEST
        elif "c-typeahead__item" in selector:
            self.offer = "選ばれた求人"
            self.tree = FORM
        elif "c-button--send" in selector:
            # 確認の段の送信。**媒体はいま本文欄に在る文面をそのまま載せて送る。**
            self.fire_request("POST", SEND_URL, self.body)
        elif "c-button--important" in selector:
            if self.offer and self.body:
                self.tree = CONFIRM
            else:
                self.tree = REJECTED


def _install(monkeypatch, page: _Page) -> None:
    import jobmedley_scout.recon.follow_send as module

    monkeypatch.setattr(module, "dom_tree", lambda p: p.tree)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)
    monkeypatch.setattr(module, "_measure_after_press", _measure)


def _measure(page: _Page, before: Any, config: Any) -> tuple[DomTree, dict[str, int]]:
    from jobmedley_scout.recon.list_structure import token_counts

    return page.tree, token_counts(page.tree)


def _run(monkeypatch, page: _Page, sentinel: str):
    _install(monkeypatch, page)
    page.gate.arm()
    try:
        return walk_form(
            page,
            tree=LIST,
            row_index=1,
            sentinel=sentinel,
            gate=page.gate,
            config=_Config(),  # type: ignore[arg-type]
        )
    finally:
        page.gate.disarm()


def test_the_walk_follows_the_taught_order_and_captures_the_send(monkeypatch) -> None:
    """**教わった順に辿れば、送信路は1回で取れる。**

    フォームを開く → 求人を選ぶ → 本文に目印 → 確認 → 送信。最後の押下で媒体は
    送信を投げようとし、武装した遮断がそれを止めて記録する。目印が本文に載って
    いるので、**遮断した非GETのどれが送信路かを観測で決められる** (原則3)。
    """
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    page = _Page(gate)
    sentinel = make_sentinel("test-run")
    outcome = _run(monkeypatch, page, sentinel)

    assert outcome.form_opened
    assert outcome.suggestions_seen
    assert outcome.offer_chosen
    assert outcome.body_written
    assert outcome.submit_pressed
    # 送信は起きない。
    assert page.sent == []
    # **押下はすべて武装の内側で起きた。** ここが1つでも False なら送信事故が起きうる。
    assert all(armed for _, armed in page.pressed), "武装の外で押した"

    walk = SendWalk(
        requested_url=URL,
        list_rendered=True,
        rows_found=True,
        gate_armed=True,
        form_opened=True,
        suggestions_seen=True,
        offer_chosen=True,
        body_written=True,
        submit_pressed=True,
        steps=outcome.steps,
    )
    assert walk.reached() is SendWalkStage.SEND_OBSERVED
    carrier = walk.carrier()
    assert carrier is not None
    assert carrier.carried_sentinel
    report = walk.render()
    assert "api.send.paid.url_pattern: UNRESOLVED" not in report
    assert "customers/scouts/{id}" in report  # 会員IDは伏せてある (13.2)
    assert "**このコマンドは送信を1件も行っていません。**" in report


def test_the_body_is_written_only_after_the_offer_is_chosen(monkeypatch) -> None:
    """**順序が仕様である。**

    テンプレートは求人を選ぶと本文を自動で埋める。先に目印を書けば上書きされ、
    目印の無い送信を観測することになる -- そうなると、遮断した非GETのどれが
    送信路かを決める根拠が消える。
    """
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    page = _Page(gate)
    order: list[str] = []
    original = _Page.press

    def _record(self: _Page, selector: str) -> None:
        if "c-typeahead__item" in selector:
            order.append("offer")
        original(self, selector)

    monkeypatch.setattr(_Page, "press", _record)

    def _fill(self: _Handle, text: str, timeout: int = 0) -> None:
        order.append("body")
        self._page.body = text

    monkeypatch.setattr(_Handle, "fill", _fill)
    _run(monkeypatch, page, make_sentinel("test-run"))

    assert order[: order.index("body") + 1].count("offer") >= 1, "本文を先に書いている"
    assert order.index("offer") < order.index("body")


def test_an_unfilled_form_is_reported_as_rejected_not_as_blocked(monkeypatch) -> None:
    """**「遮断で止まった」と「そもそも送られなかった」を分ける。**

    どちらも報告の上では「目印を運ぶ非GETが無い」だが、次の一手はまるで違う
    (実測12回目)。候補が1件も出なければ求人を選べず、必須欄が埋まらない。
    """
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    page = _Page(gate)

    def _no_suggestions(self: _Page, selector: str) -> None:
        self.pressed.append((selector, self.gate.is_armed))
        if "js-tour-guide-scout-button" in selector:
            self.tree = FORM
        # 求人の欄を押しても候補は出ない (実物でそうなる可能性は残っている)。

    monkeypatch.setattr(_Page, "press", _no_suggestions)
    outcome = _run(monkeypatch, page, make_sentinel("test-run"))

    assert outcome.form_opened
    assert not outcome.suggestions_seen
    assert not outcome.offer_chosen

    walk = SendWalk(
        requested_url=URL,
        list_rendered=True,
        rows_found=True,
        gate_armed=True,
        form_opened=True,
        steps=outcome.steps,
    )
    assert walk.reached() is SendWalkStage.NO_SUGGESTIONS
    report = walk.render()
    assert "候補が1件も現れませんでした" in report
    assert "api.send.paid.url_pattern: UNRESOLVED" in report
    assert page.sent == []


def test_a_send_with_no_confirm_step_still_reports_success(monkeypatch) -> None:
    """**確認の段は「段」であって「関門」ではない。**

    押したら即座に送る作りもありうる。確認の段を鎖の一段にすると、確認が無いまま
    送信を観測した実行で単調性が破れ、**成功したのに報告が例外で落ちる**。
    """
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    page = _Page(gate)

    def _direct(self: _Page, selector: str) -> None:
        self.pressed.append((selector, self.gate.is_armed))
        if "js-tour-guide-scout-button" in selector:
            self.tree = FORM
        elif selector.startswith("input"):
            self.tree = SUGGEST
        elif "c-typeahead__item" in selector:
            self.offer = "選ばれた求人"
            self.tree = FORM
        elif "c-button--important" in selector:
            # 確認の段を挟まず、その場で送る。
            self.fire_request("POST", SEND_URL, self.body)

    monkeypatch.setattr(_Page, "press", _direct)
    outcome = _run(monkeypatch, page, make_sentinel("test-run"))

    walk = SendWalk(
        requested_url=URL,
        list_rendered=True,
        rows_found=True,
        gate_armed=True,
        form_opened=outcome.form_opened,
        suggestions_seen=outcome.suggestions_seen,
        offer_chosen=outcome.offer_chosen,
        body_written=outcome.body_written,
        submit_pressed=outcome.submit_pressed,
        steps=outcome.steps,
    )
    assert walk.reached() is SendWalkStage.SEND_OBSERVED  # 例外にならない
    assert page.sent == []


def test_the_ladder_refuses_to_report_a_state_that_cannot_have_happened() -> None:
    """**鎖の単調性を破る状態は嘘である。**

    後の工程の証拠が立っているのに前の工程が False なら、実行のどこかが事実と
    違えてブール値を立てている。その状態で報告を出せば必ず嘘になるので、
    報告せず止める (実測2回目の再発防止)。
    """
    import pytest

    walk = SendWalk(
        requested_url=URL,
        list_rendered=False,  # 一覧が出ていないのに…
        rows_found=True,  # …行は見つかったことになっている
    )
    with pytest.raises(ValueError, match="時系列と矛盾"):
        walk.reached()


def test_the_body_carries_this_run_s_sentinel(monkeypatch) -> None:
    """目印は実行ごとに違う。**前の実行の残骸を送信路と誤認しない。**"""
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    page = _Page(gate)
    sentinel = make_sentinel("test-run")
    _run(monkeypatch, page, sentinel)
    assert page.body == sentinel_body(sentinel)


def test_every_step_that_did_not_happen_is_shown_as_not_done() -> None:
    """報告は段ごとに ○ / × を出す。**黙って落とさない** (原則2)。"""
    walk = SendWalk(
        requested_url=URL,
        list_rendered=True,
        rows_found=True,
        gate_armed=True,
        steps=(
            Step(name="送信フォームを開く", done=True, selector="button.c-button"),
            Step(
                name="スカウト対象求人の欄を押して候補を出す",
                done=False,
                detail="候補が現れませんでした",
            ),
        ),
        form_opened=True,
    )
    report = walk.render()
    assert "○ 送信フォームを開く" in report
    assert "× スカウト対象求人の欄を押して候補を出す" in report
