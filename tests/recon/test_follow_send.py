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

    def evaluate(self, script: str, arg: object = None) -> object:
        """DOM を直に触る経路。**押下では焦点は移らない、を写し取る。**

        実測18回目、求人の欄は「押した」のに候補が1件も出なかった。押下は
        ``dispatch_event`` で届いているが、それは焦点を移さないので入力補完は
        開かない。**偽物もそう振る舞わないと、直したことの検証にならない。**
        """
        from jobmedley_scout.recon.follow_send import _FOCUS_JS, _TYPE_JS

        page = self._page
        if script == _FOCUS_JS:
            page.focused = self._selector
            return page.on_focus(self._selector)
        if script == _TYPE_JS:
            page.typed.append((self._selector, str(arg)))
            page.on_type(self._selector, str(arg))
            return True
        if "el.type" in script:
            # クラス名ではなく HTML の意味で判定する側の入口。
            if not self._selector.startswith("input"):
                return "textarea"
            return "checkbox" if "checkbox" in self._selector else "text"
        raise RuntimeError("この偽物が知らない評価です")


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
        self.typed: list[tuple[str, str]] = []
        self.focused = ""
        #: 焦点を当てただけで候補が出る作りか。既定は **出ない** --
        #: 実測18回目に見たのは「触っただけでは出ない」側だからである。
        self.suggests_on_focus = False

    def on_focus(self, selector: str) -> bool:
        if self.suggests_on_focus and selector.startswith("input"):
            self.tree = SUGGEST
        return True

    def on_type(self, selector: str, text: str) -> None:
        # **空の検索では候補が出ない。** 文字が要る作りを写す。
        if selector.startswith("input") and text.strip():
            self.tree = SUGGEST

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    def fire_request(self, method: str, url: str, body: str) -> None:
        if self.gate.decide(method, url, body, {}) is GateDecision.PASS:
            self.sent.append(url)  # **これが起きたら送信されたということ**

    def press(self, selector: str) -> None:
        self.pressed.append((selector, self.gate.is_armed))
        if "js-tour-guide-scout-button" in selector:
            self.tree = FORM
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

    # **どの文字を打っても候補が出ない。** 実測18回目に見たのは「押しただけでは
    # 出ない」側だったが、打っても出ない作りはありうる。そのときに何を報告するか
    # が、この試験が固定していることである。
    monkeypatch.setattr(_Page, "press", _no_suggestions)
    monkeypatch.setattr(_Page, "on_type", lambda self, selector, text: None)
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


def test_the_field_is_focused_not_merely_pressed(monkeypatch) -> None:
    """**押下では焦点は移らない。**

    実測18回目、求人の欄は3回「押した」のに候補が1件も出なかった。押下は
    ``dispatch_event`` で届けているが、それは ``document.activeElement`` を
    動かさない -- 入力補完は ``focus`` か ``input`` で開くので、クリックの
    イベントだけでは何も起きない。

    直したことの証拠は「候補が出た」ではなく **「焦点を当てた」** である。
    出たかどうかは媒体の都合だが、当てたかどうかはこちらの責任である。
    """
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    page = _Page(gate)
    outcome = _run(monkeypatch, page, make_sentinel("test-run"))

    assert page.focused.startswith("input"), "求人の欄に焦点を当てていない"
    assert outcome.suggestions_seen
    assert outcome.offer_chosen


def test_focus_alone_is_enough_when_the_site_opens_on_focus(monkeypatch) -> None:
    """**出るなら打たない。**

    空の検索で全件返す作りなら、焦点を当てただけで候補が出る。そのとき欄へ
    文字を打つのは余計な操作であり、打った文字が候補を絞ってしまう。
    """
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    page = _Page(gate)
    page.suggests_on_focus = True
    outcome = _run(monkeypatch, page, make_sentinel("test-run"))

    assert outcome.offer_chosen
    assert page.typed == [], "候補が出ているのに文字を打った"


def test_a_typeahead_that_needs_text_is_probed_and_the_probe_is_reported(monkeypatch) -> None:
    """**打った文字を報告する。**

    どの入力で候補が出たかは、次の実行が推測ではなく観測から始めるための材料で
    ある。報告に出ないと、直った理由が分からないまま先へ進むことになる。
    """
    from jobmedley_scout.recon.follow_send import SUGGESTION_PROBES

    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    page = _Page(gate)  # 既定は「焦点だけでは出ない」
    outcome = _run(monkeypatch, page, make_sentinel("test-run"))

    assert page.typed, "文字を1つも打っていない"
    # 空の試行から順に、**広い順に** 試している。
    assert [text for _, text in page.typed] == [probe for probe in SUGGESTION_PROBES if probe][
        : len(page.typed)
    ]

    walk = SendWalk(
        requested_url=URL,
        list_rendered=True,
        rows_found=True,
        gate_armed=True,
        form_opened=True,
        suggestions_seen=True,
        offer_chosen=outcome.offer_chosen,
        body_written=outcome.body_written,
        submit_pressed=outcome.submit_pressed,
        steps=outcome.steps,
    )
    report = walk.render()
    assert "打ち込んだ文字:" in report, "何を打ったかが報告に出ていない"
    assert "焦点だけ" in report, "焦点だけの試行が報告に出ていない"


def test_a_checkbox_is_never_tried_as_the_search_field(monkeypatch) -> None:
    """**試す価値の無いものを試さない。**

    実測18回目、フォームの中の ``input`` を順に触っていったら、2番目と3番目は
    送信先のチェックボックスだった。押しても候補は出ないし、外せば送信先が
    消える。「候補が出ない」という同じ失敗を3回並べても、報告は水増しされる
    だけで何も分からない。
    """
    from jobmedley_scout.recon.form_structure import query_fields_in
    from jobmedley_scout.recon.list_structure import subtree_sizes

    sizes = subtree_sizes(FORM)
    root = 6  # フォームの器
    fields = query_fields_in(FORM, sizes, root)
    assert all("checkbox" not in token for f in fields for token in f.tokens)
