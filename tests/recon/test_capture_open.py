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

import pytest

from jobmedley_scout.browser.dom import DomNode, DomTree
from jobmedley_scout.recon.capture_open import (
    AttemptResult,
    OpenObservation,
    StopStage,
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
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(verified, unverified),
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
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(attempt,),
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
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(attempt,),
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
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(attempt,),
    ).render()

    assert "48211" not in report
    assert "/customers/members/{id}/scouts" in report


# --- 実測1回目 (capture-open) が暴いた2つの欠陥 --------------------------------


def test_a_run_where_the_list_never_rendered_presses_nothing() -> None:
    """**実測1回目の失敗。** 遷移の前に武装したせいで一覧のデータ読み込み (非GET)
    まで止まり、カードが1枚も描画されなかった。それでも探索は進み、
    「最も繰り返している構造」としてヘッダを選び、**サイトのロゴを押した**。

    送信は起きなかった (遮断が効いていた) が、観測としては無意味である。
    描画を確かめる前に押さない。
    """
    report = OpenObservation(
        requested_url=URL,
        list_rendered=False,
        note="一覧の行 (div.c-search-member-card) が現れませんでした。",
        landed_url=URL,
    ).render()

    assert "ボタンを1つも押していません" in report
    assert "一覧が描画されなかった" in report
    assert "div.c-search-member-card" in report


def test_a_press_that_navigates_away_stops_the_exploration(monkeypatch) -> None:
    """別画面へ遷移したら打ち切る。**武装したまま知らない画面を押し進めない。**

    再遷移で戻すこともできない -- 武装中は一覧のデータ読み込みが止まるので、
    戻った先には押す対象が無い (それが実測1回目の失敗そのものだった)。
    """
    tree = _card_page_tree()
    gate = SendGate()
    page = _FakePage(gate, tree)
    _install(monkeypatch, page, tree)
    # 1押し目でURLが変わる作りにする。
    original = _FakeLocatorHandle.click

    def _click_then_navigate(self, timeout: int = 0) -> None:
        original(self, timeout)
        type(self._page).url = "https://customers.job-medley.com/customers/members/1"

    monkeypatch.setattr(_FakeLocatorHandle, "click", _click_then_navigate)

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
        type(page).url = URL  # 他のテストへ漏らさない

    assert len(results) == 1  # 2つ目は押していない
    assert results[0].navigated is True


def test_navigation_is_reported_as_navigation_not_as_a_missing_drawer() -> None:
    """「開かなかった」と「別画面へ移った」は別の事実である (原則3)。"""
    attempt = AttemptResult(
        selector="button.c-button",
        nth=0,
        looks_like_send=False,
        clicked=True,
        gained=(),
        navigated=True,
    )
    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        landed_url="https://customers.job-medley.com/customers/members/1",
        attempts=(attempt,),
    ).render()

    assert "別画面へ遷移" in report
    assert "この座標は不要かもしれません" in report


# --- 実測2回目 (capture-open) が暴いた2つの欠陥 --------------------------------


def test_a_card_nested_inside_another_repeat_is_still_found(monkeypatch) -> None:
    """**実測2回目の失敗。** 一覧は正しく描画され、座標での確認も通ったのに、
    行が「取れない」と言われた。

    原因は繰り返し構造の解析を経由していたこと。``row_group_candidates`` は
    極大性の規則で「外側の繰り返しに含まれる群」を落とすので、カードが別の
    繰り返し構造 (行を包む枠など) の内側にあると消える -- 実測2回目
    (observe-list) で行が ``div.c-segment`` に化けたのと同じ規則である。

    **座標が確定しているのだから、推定を経由せず直接指す。**
    """
    # カードが「繰り返す枠」の内側にある形。極大性ならカードが落ちる。
    tree = _tree(
        ("body", ("c-body",), -1),  # 0
        ("div", ("result-table__body",), 0),  # 1
        ("div", ("row-wrap",), 1),  # 2  <- 繰り返す枠 (外側)
        ("div", ("c-search-member-card",), 2),  # 3  <- 本当の行
        ("button", ("c-button",), 3),  # 4
        ("div", ("row-wrap",), 1),  # 5
        ("div", ("c-search-member-card",), 5),  # 6
        ("button", ("c-button",), 6),  # 7
    )
    from jobmedley_scout.recon.list_structure import (
        indices_with_token,
        row_group_candidates,
        subtree_sizes,
    )

    # 旧実装が使っていた経路では、カードの群は極大性で落ちる。
    groups = row_group_candidates(tree, subtree_sizes(tree), [])
    assert all(g.token != "div.c-search-member-card" for g in groups)

    # 座標のトークンで直接指せば取れる。
    assert indices_with_token(tree, "div.c-search-member-card") == (3, 6)


def test_a_run_that_could_not_take_rows_says_so_not_that_arming_failed() -> None:
    """**報告の嘘を塞ぐ。** 実測2回目は「遮断を武装できなかったため」と印字したが、
    武装はそもそも行を取った後に行われる。本当の理由は行を取れなかったことで、
    それは補足行にしか出ていなかった。

    検査は時系列の順に並べる -- 武装の検査を先に置くと、その前で止まった実行に
    誤った理由が付く。
    """
    report = OpenObservation(
        requested_url=URL,
        tree_read=True,
        list_rendered=True,
        rows_found=False,
        gate_armed=False,  # 武装まで到達していないので False のまま
        note="行 div.c-search-member-card は画面に在りましたが、木からは取れませんでした。",
    ).render()

    assert "候補者の行を取れなかったため" in report
    assert "遮断を武装できなかった" not in report
    assert "木からは取れませんでした" in report


def test_a_run_that_really_failed_to_arm_still_says_arming_failed() -> None:
    """順序を入れ替えても、武装の失敗そのものは正しく報告される。"""
    report = OpenObservation(
        requested_url=URL,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        gate_armed=False,
        note="武装の確認に失敗しました。",
    ).render()

    assert "遮断を武装できなかった" in report


# --- 反嘘の構造保証 (実測2回目の再発防止) --------------------------------------
#
# 「報告が事実と違う」を実測4回目・7回目・capture-open 2回目と繰り返した。
# 共通の原因は、報告が独立したブール値を手で並べた順に検査していたこと。
# いまは到達地点を1つの値 (reached) に集約し、報告はそれだけを見る。
# ここでその集約が嘘をつけないことを固定する。


def _stopped_at(stage: StopStage) -> OpenObservation:
    """ちょうど ``stage`` で止まった、単調性を保った観測を作る。"""
    flags = {
        StopStage.NO_SESSION: {"session_present": False},
        StopStage.SESSION_EXPIRED: {"session_expired": True},
        StopStage.NOT_RENDERED: {"list_rendered": False},
        StopStage.TREE_UNREAD: {"list_rendered": True, "tree_read": False},
        StopStage.NO_ROWS: {"list_rendered": True, "tree_read": True, "rows_found": False},
        StopStage.ARM_FAILED: {
            "list_rendered": True,
            "tree_read": True,
            "rows_found": True,
            "gate_armed": False,
        },
        StopStage.EXPLORED: {
            "list_rendered": True,
            "tree_read": True,
            "rows_found": True,
            "gate_armed": True,
        },
    }[stage]
    return OpenObservation(requested_url=URL, **flags)  # type: ignore[arg-type]


def test_each_stage_reports_itself_and_no_later_stage() -> None:
    """**各停止地点は、自分の理由だけを出し、後の工程の失敗を語らない。**

    実測2回目は「行を取れなかった」実行が「武装できなかった」と言った。ここで
    全停止地点について、その理由が主文に出て、かつ **後の工程の理由が出ない**
    ことを固定する。
    """
    # 各停止地点の主文に必ず出る語 (その工程を指す) と、出てはいけない語。
    signature = {
        StopStage.NO_SESSION: "保存セッションがありません",
        StopStage.SESSION_EXPIRED: "セッションが効いていません",
        StopStage.NOT_RENDERED: "一覧が描画されなかった",
        StopStage.TREE_UNREAD: "DOMの木を読めませんでした",
        StopStage.NO_ROWS: "候補者の行を取れなかった",
        StopStage.ARM_FAILED: "遮断を武装できなかった",
    }
    for stage, phrase in signature.items():
        observed = _stopped_at(stage)
        assert observed.reached() is stage
        report = observed.render()
        assert phrase in report, f"{stage.value} の主文が出ていない"
        # 後の工程の理由が混ざっていないこと。
        for later_stage, later_phrase in signature.items():
            if list(signature).index(later_stage) > list(signature).index(stage):
                assert (
                    later_phrase not in report
                ), f"{stage.value} で止まったのに {later_stage.value} の理由が出ている"


def test_the_row_stall_never_blames_arming() -> None:
    """実測2回目そのもの: 行で止まった実行は武装を理由にしない。"""
    report = _stopped_at(StopStage.NO_ROWS).render()
    assert "候補者の行を取れなかった" in report
    assert "武装できなかった" not in report


def test_an_inconsistent_state_refuses_to_render_a_lie() -> None:
    """**単調性を破る状態は嘘なので、報告せず例外にする** (握り潰さない)。

    行を取れていない (rows_found=False) のに武装済み (gate_armed=True) を主張する
    状態は、実行のどこかがブール値を事実と違えて立てている。この状態から
    「もっともらしい報告」を出すと必ず嘘になる。
    """
    liar = OpenObservation(
        requested_url=URL,
        list_rendered=True,
        tree_read=True,
        rows_found=False,  # 行は取れていない
        gate_armed=True,  # なのに武装済みを主張 = 事実と矛盾
    )
    with pytest.raises(ValueError, match="時系列と矛盾"):
        liar.reached()
    with pytest.raises(ValueError, match="時系列と矛盾"):
        liar.render()


# --- 実測3回目 (capture-open): 現れたものを辿って送信路へ ------------------------


class _ChainPage(_FakePage):
    """チェックボックス → 一括スカウトバー → スカウトボタン、という実測の導線。

    押した結果 DOM が変わる作りを最小限に再現する。``dom_tree`` はテスト側で
    ``page.tree`` を返すよう差し替える。
    """

    def __init__(self, gate: SendGate) -> None:
        self._card_only = _tree(
            ("body", ("c-body",), -1),
            ("div", ("c-search-member-card",), 0),
            ("label", ("c-checkbox",), 1),
        )
        self._with_bar = _tree(
            ("body", ("c-body",), -1),
            ("div", ("c-search-member-card",), 0),
            ("label", ("c-checkbox",), 1),
            ("div", ("c-sticky-scout-bar",), 0),
            ("button", ("c-sticky-scout-bar__scout-button",), 3),
        )
        super().__init__(gate, self._card_only)
        self.tree = self._card_only

    def locator(self, selector: str) -> _FakeLocator:  # type: ignore[override]
        return _FakeLocator(self, selector)


def test_the_exploration_follows_the_chain_to_the_scout_button(monkeypatch) -> None:
    """**実測3回目そのもの。** チェックボックスを押すと一括スカウトバーが現れる。

    以前はここで「閉じられないから」打ち切っていたので、導線の1歩目で止まった。
    閉じられないのはバグではなく **閉じるものではなかったから** である。
    現れたものを次に押すことで、送信路のボタンまで到達する。
    """
    import jobmedley_scout.recon.capture_open as module

    gate = SendGate()
    page = _ChainPage(gate)

    def _click(self, timeout: int = 0) -> None:
        page.clicks.append((self._selector, self._index, page.gate.is_armed))
        if "checkbox" in self._selector:
            page.tree = page._with_bar  # 選択するとバーが現れる
        if "scout" in self._selector:
            page.fire_request("POST", "https://customers.job-medley.com/api/scouts", "S")

    monkeypatch.setattr(_FakeLocatorHandle, "click", _click)
    monkeypatch.setattr(module, "dom_tree", lambda p: p.tree)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_interactive", lambda *a, **k: None)
    monkeypatch.setattr(module, "goto", lambda *a, **k: None)
    monkeypatch.setattr(module, "_dismiss_tour", lambda *a, **k: None)
    monkeypatch.setattr(module, "_close_landing_modals", lambda *a, **k: None)

    gate.arm()
    try:
        results = explore_card_actions(
            page,
            tree=page._card_only,
            row_index=1,
            sentinel="S",
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
        )
    finally:
        gate.disarm()

    pressed = [r.selector for r in results]
    assert any("checkbox" in s for s in pressed)  # 1歩目
    assert any("scout-button" in s for s in pressed), "現れたバーのボタンまで辿れていない"
    assert page.sent == []  # **送信は起きない** (遮断が効いている)
    # 遮断した通信が観測として返る = 送信路の正体が分かる。
    assert any(e.carried_sentinel for r in results for e in r.blocked)


# --- 報告の正直さ (実測3回目が露呈させた2点) ------------------------------------


def test_a_failed_click_that_changed_the_page_reports_both_facts() -> None:
    """**どちらの事実も消さない。** 実測3回目はクリックが完了しなかったのに
    画面の構造が17種増えた。「押せなかった」だけだと変化を、「押せた」だけだと
    完了しなかった事実を握り潰す。
    """
    attempt = AttemptResult(
        selector="label.c-checkbox",
        nth=1,
        looks_like_send=False,
        clicked=False,
        gained=("div.c-sticky-scout-bar",),
    )
    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(attempt,),
    ).render()

    assert "クリックは完了しませんでした" in report
    assert "画面の構造は変化しました" in report


def test_an_unverified_region_is_not_called_a_drawer() -> None:
    """観測したのは「構造が増えた」であって、それがドロワーかは分かっていない。

    実測3回目で増えたのは一括スカウト用の選択バーで、閉じるものですらなかった。
    """
    attempt = AttemptResult(
        selector="label.c-checkbox",
        nth=1,
        looks_like_send=False,
        clicked=True,
        gained=("div.c-sticky-scout-bar",),
        close_verified=None,
    )
    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(attempt,),
    ).render()

    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "ドロワーは開きました" not in report
    assert "閉じられる領域 (ドロワー/モーダル) かは確認できていません" in report


# --- 実測4回目: 8個すべてクリックが完了しなかった --------------------------------


def test_a_click_that_times_out_is_still_delivered_and_says_why(monkeypatch) -> None:
    """**実測4回目そのもの。** 画面下部に固定表示される一括操作バーがあると、
    Playwright の操作可能性検査 (見えて・動かず・有効で・イベントを受け取れる) が
    満了し続け、**一度も押せない**。1個目で画面が変化していたので、押下自体は
    伝わっていた。

    通常のクリックが満了したら DOM イベントを直接発火して押下を届ける。理由の
    分類も必ず報告する -- 握り潰すと次の手が打てない。
    """

    tree = _card_page_tree()
    gate = SendGate()
    page = _FakePage(gate, tree)
    _install(monkeypatch, page, tree)

    def _always_times_out(self, timeout: int = 0) -> None:
        raise TimeoutError('<div class="c-sticky-scout-bar">…</div> intercepts pointer events')

    def _dispatch(self, event: str, timeout: int = 0) -> None:
        page.clicks.append((self._selector, self._index, page.gate.is_armed))
        if "scout" in self._selector:
            page.fire_request("POST", "https://customers.job-medley.com/api/scouts", "S")

    monkeypatch.setattr(_FakeLocatorHandle, "click", _always_times_out)
    monkeypatch.setattr(_FakeLocatorHandle, "dispatch_event", _dispatch, raising=False)

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

    assert results, "1つも試していない"
    assert all(not r.clicked for r in results)  # 通常のクリックは通らない
    assert all(r.dispatched for r in results)  # それでも押下は届いた
    assert all(r.failure_kind == "覆われていて押下が届かない" for r in results)
    # **届いた押下は、武装した遮断の内側にある。** 送信は起きない。
    assert page.sent == []
    assert any(e.carried_sentinel for r in results for e in r.blocked)


def test_the_report_states_the_failure_reason_and_whether_it_was_delivered() -> None:
    """「完了しませんでした」だけでは次の手が打てない。**理由と到達可否を出す。**"""
    delivered = AttemptResult(
        selector="label.c-checkbox",
        nth=1,
        looks_like_send=False,
        clicked=False,
        failure_kind="覆われていて押下が届かない",
        dispatched=True,
        gained=("div.c-sticky-scout-bar",),
    )
    not_delivered = AttemptResult(
        selector="button.c-button",
        nth=0,
        looks_like_send=False,
        clicked=False,
        failure_kind="要素が見えない",
        dispatched=False,
    )
    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(delivered, not_delivered),
    ).render()

    assert "覆われていて押下が届かない" in report
    assert "押下をDOMイベントで直接届けました" in report
    assert "要素が見えない" in report
    assert "押下は届いていません" in report


# --- 目印を書き込まない実行が、書き込んだふりをしない --------------------------


def test_a_run_that_wrote_no_sentinel_says_so_instead_of_blaming_the_send_screen() -> None:
    """**このコマンドは目印を自動では書けないことがある。**

    以前の報告は、目印を運ぶ非GETが無いことを「送信画面まで到達していない」と
    説明していた。しかし目印を1文字も書いていない実行では、**到達したかどうか
    自体が観測されていない**。書いていないから載っていないだけである。

    その説明は、すぐ上に並んでいる「遮断した非GET」の一覧と矛盾しうる --
    送信APIを実際に叩いていても「到達していない」と書いてしまう。報告が事実と
    食い違うことは、この工程で最もしてはいけないことである。
    """
    attempt = AttemptResult(
        selector="button.c-sticky-scout-bar__scout-button",
        nth=0,
        looks_like_send=True,
        clicked=True,
        sentinel_written=False,
        blocked=(
            BlockedRequest(
                method="POST",
                url="https://customers.job-medley.com/customers/scouts/12345",
                carried_sentinel=False,
            ),
        ),
    )
    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(attempt,),
    ).render()

    assert "送信画面まで到達していない" not in report, "観測していないことを理由にしている"
    assert "目印を1文字も書き込めていません" in report
    # 推測で埋めない (原則3)。候補は候補として出す。
    assert "api.send.paid.url_pattern: UNRESOLVED" in report
    assert "候補: POST" in report


def test_a_run_that_wrote_the_sentinel_and_saw_nothing_says_that_instead() -> None:
    """書いたうえで運ばれなかったのは、**意味のある観測** である。"""
    attempt = AttemptResult(
        selector="button.c-sticky-scout-bar__scout-button",
        nth=0,
        looks_like_send=True,
        clicked=True,
        sentinel_written=True,
        blocked=(
            BlockedRequest(method="POST", url="https://example.com/beacon", carried_sentinel=False),
        ),
    )
    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(attempt,),
    ).render()

    assert "入力欄に目印を書き込んだうえで押しましたが" in report
    assert "目印を1文字も書き込めていません" not in report
    assert "api.send.paid.url_pattern: UNRESOLVED" in report


# --- 現れた領域に書き込んでから押す ---------------------------------------------


class _ComposePage(_FakePage):
    """チェックボックス → バー (本文欄つき) → 送信、という導線。"""

    def __init__(self, gate: SendGate) -> None:
        self._card_only = _tree(
            ("body", ("c-body",), -1),
            ("div", ("c-search-member-card",), 0),
            ("label", ("c-checkbox",), 1),
        )
        self._with_bar = _tree(
            ("body", ("c-body",), -1),
            ("div", ("c-search-member-card",), 0),
            ("label", ("c-checkbox",), 1),
            ("div", ("c-sticky-scout-bar",), 0),
            ("textarea", ("c-scout-form__body",), 3),
            ("button", ("c-sticky-scout-bar__scout-button",), 3),
        )
        super().__init__(gate, self._card_only)
        self.tree = self._card_only
        self.fills: list[tuple[str, str, bool]] = []
        self.body_text = ""

    def locator(self, selector: str) -> _FakeLocator:  # type: ignore[override]
        return _ComposeLocator(self, selector)


class _ComposeLocator(_FakeLocator):
    def count(self) -> int:
        return 1

    def nth(self, index: int) -> _FakeLocatorHandle:
        return _ComposeHandle(self._page, self._selector, index)


class _ComposeHandle(_FakeLocatorHandle):
    def fill(self, text: str, timeout: int = 0) -> None:
        page = self._page
        if not self._selector.startswith("textarea"):
            raise RuntimeError("Element is not an <input>, <textarea> or [contenteditable]")
        page.fills.append((self._selector, text, page.gate.is_armed))  # type: ignore[attr-defined]
        page.body_text = text  # type: ignore[attr-defined]

    def input_value(self) -> str:
        # **書いたあとに読み直すのが本番の作りである。** ``fill`` が通ったことは
        # 値が残ったことではないので、実装は必ずここを読む。偽物も同じ形にする。
        if not self._selector.startswith("textarea"):
            raise RuntimeError("Element is not an <input>, <textarea> or [contenteditable]")
        return self._page.body_text  # type: ignore[attr-defined,no-any-return]

    def click(self, timeout: int = 0) -> None:
        page = self._page
        page.clicks.append((self._selector, self._index, page.gate.is_armed))
        if "checkbox" in self._selector:
            page.tree = page._with_bar  # type: ignore[attr-defined]
        if "scout-button" in self._selector:
            # 媒体は、いま欄に入っている文面をそのまま本文に載せて送信する。
            page.fire_request(
                "POST",
                "https://customers.job-medley.com/customers/scouts/12345",
                page.body_text,  # type: ignore[attr-defined]
            )


def test_the_sentinel_is_written_into_the_revealed_region_before_pressing(monkeypatch) -> None:
    """**書き込まなければ、どの非GETが送信路かは永遠に分からない。**

    遮断は非GETを全部止めて記録する (fail-closed)。1押しで複数の非GETが出るので、
    その中から送信路を選ぶ根拠が要る。根拠は「自分が書いた目印がその本文に
    載っていること」だけである。

    書き込みは送信ではない。そして書き込みも押下も、武装した遮断の内側で起きる。
    """
    import jobmedley_scout.recon.capture_open as module
    from jobmedley_scout.recon.sentinel import make_sentinel, sentinel_body

    gate = SendGate()
    page = _ComposePage(gate)
    sentinel = make_sentinel("test-run")

    monkeypatch.setattr(module, "dom_tree", lambda p: p.tree)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_interactive", lambda *a, **k: None)
    monkeypatch.setattr(module, "goto", lambda *a, **k: None)
    monkeypatch.setattr(module, "_dismiss_tour", lambda *a, **k: None)
    monkeypatch.setattr(module, "_close_landing_modals", lambda *a, **k: None)

    gate.arm()
    try:
        results = explore_card_actions(
            page,
            tree=page._card_only,
            row_index=1,
            sentinel=sentinel,
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
        )
    finally:
        gate.disarm()

    # 本文欄に、この実行の目印が書き込まれた。
    assert page.fills, "現れた領域の入力欄に何も書いていない"
    assert all(text == sentinel_body(sentinel) for _, text, _ in page.fills)
    # **書き込みも武装の内側で起きている。**
    assert all(armed for _, _, armed in page.fills)
    # 送信は起きない。
    assert page.sent == []

    # 送信ボタンの押下が、書いた目印を運ぶ非GETとして観測された。
    scout = [r for r in results if "scout-button" in r.selector]
    assert scout, "現れたバーのボタンまで辿れていない"
    assert scout[0].sentinel_written
    assert any(e.carried_sentinel for e in scout[0].blocked)

    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=results,
    ).render()
    assert "api.send.paid.url_pattern: UNRESOLVED" not in report
    assert "customers/scouts/{id}" in report  # 会員IDは伏せてある (13.2)


class _StubbornHandle(_ComposeHandle):
    """A textarea that ``fill`` can never reach. **実測16回目の再現。**

    スカウトのサイドカバーでは、押下も書き込みも Playwright の可視性検査を
    最後まで通らなかった。押下には ``dispatch_event`` という逃げ道があったが、
    書き込みには無く、目印は一度も入らないまま送信ボタンだけが押された。
    """

    #: DOM経由の書き込みまで失敗させるか (書けない欄そのものの再現)。
    refuse_dom_write = False

    def fill(self, text: str, timeout: int = 0) -> None:
        raise RuntimeError("Element is not visible")

    def evaluate(self, script: str, arg: object = None) -> object:
        if not self._selector.startswith("textarea"):
            return False
        if type(self).refuse_dom_write:
            return False
        page = self._page
        page.fills.append((self._selector, str(arg), page.gate.is_armed))  # type: ignore[attr-defined]
        page.body_text = str(arg)  # type: ignore[attr-defined]
        return True


class _StubbornLocator(_ComposeLocator):
    def nth(self, index: int) -> _FakeLocatorHandle:
        return _StubbornHandle(self._page, self._selector, index)


class _StubbornPage(_ComposePage):
    def locator(self, selector: str) -> _FakeLocator:  # type: ignore[override]
        return _StubbornLocator(self, selector)


def _explore(page: _FakePage, monkeypatch, sentinel: str = "ZZRECON-STUBBORN") -> tuple:
    import jobmedley_scout.recon.capture_open as module

    monkeypatch.setattr(module, "dom_tree", lambda p: p.tree)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_interactive", lambda *a, **k: None)
    monkeypatch.setattr(module, "goto", lambda *a, **k: None)
    monkeypatch.setattr(module, "_dismiss_tour", lambda *a, **k: None)
    monkeypatch.setattr(module, "_close_landing_modals", lambda *a, **k: None)

    page.gate.arm()
    try:
        return explore_card_actions(
            page,
            tree=page._card_only,  # type: ignore[attr-defined]
            row_index=1,
            sentinel=sentinel,
            gate=page.gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
        )
    finally:
        page.gate.disarm()


def test_the_sentinel_reaches_a_field_that_fill_cannot_touch(monkeypatch) -> None:
    """**押下に最後の手段があるなら、書き込みにも要る。**

    ``fill`` は「見えて・有効で・編集できる」まで待つ。その検査が通らない欄では、
    以前の実装は黙って諦めていた -- 報告に残るのは「書ける部品が1個あったのに
    1つも書けなかった」だけで、理由も、次の一手も分からなかった。

    DOM へ直接書けば目印は入る。そして **どう書けたかも報告する** (直接書きに
    頼っている間は、人間が同じ手順を再現できる保証が無い)。
    """
    from jobmedley_scout.recon.sentinel import make_sentinel, sentinel_body

    _StubbornHandle.refuse_dom_write = False
    page = _StubbornPage(SendGate())
    sentinel = make_sentinel("test-run")
    results = _explore(page, monkeypatch, sentinel)

    assert page.fills, "DOM経由でも目印が入っていない"
    assert all(text == sentinel_body(sentinel) for _, text, _ in page.fills)
    assert all(armed for _, _, armed in page.fills), "書き込みが武装の外で起きた"
    assert page.sent == [], "送信が起きた"

    scout = [r for r in results if "scout-button" in r.selector]
    assert scout, "現れたバーのボタンまで辿れていない"
    assert scout[0].sentinel_written
    assert scout[0].sentinel_forced, "直接書きに頼った事実が残っていない"
    assert not scout[0].write_failure_kind
    # 目印が入ったので、遮断した非GETの中から送信路を **観測で** 選べる。
    assert any(e.carried_sentinel for e in scout[0].blocked)

    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=results,
    ).render()
    assert "DOMへ直接書きました" in report


def test_a_field_that_cannot_be_written_reports_why(monkeypatch) -> None:
    """**書けなかった理由を握り潰さない。**

    「書ける部品が1個ありましたが、1つも書き込めませんでした」だけでは、欄を
    探し直すのか書き方を変えるのかが決められない -- 原則2 の「静かなゼロ件」と
    同じ形である。押下の失敗には理由を付けているのだから、書き込みにも付ける。
    """
    _StubbornHandle.refuse_dom_write = True
    try:
        page = _StubbornPage(SendGate())
        results = _explore(page, monkeypatch)
    finally:
        _StubbornHandle.refuse_dom_write = False

    assert page.fills == [], "書けていないのに書けたことになっている"
    scout = [r for r in results if "scout-button" in r.selector]
    assert scout, "現れたバーのボタンまで辿れていない"
    assert not scout[0].sentinel_written
    assert scout[0].text_fields_seen == 1, "欄が在った事実まで消えている"
    assert scout[0].write_failure_kind == "要素が見えない"

    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=results,
    ).render()
    assert "書けなかった理由: 要素が見えない" in report
    # **「無かった」と取り違えない。** 欄は在ったのだから、そう書く。
    assert "書き込める入力欄が無かった" not in report
    assert "書ける入力欄は在りましたが、書き込みが届きませんでした" in report


def test_a_value_that_does_not_stay_is_not_reported_as_written(monkeypatch) -> None:
    """**``fill`` が通ったことは、値が残ったことではない。**

    値を自前で管理する作りでは、書いた直後に元へ戻されることがある。戻された
    のに「書き込みました」と報告すると、次の押下で遮断した非GETに目印が載って
    いない理由を「送信路ではないから」と読み違える -- 観測が嘘になる。
    """

    class _RevertingHandle(_ComposeHandle):
        def fill(self, text: str, timeout: int = 0) -> None:
            return None  # 受け取るが、値は残さない

        def input_value(self) -> str:
            return ""

        def evaluate(self, script: str, arg: object = None) -> object:
            return False

    class _RevertingLocator(_ComposeLocator):
        def nth(self, index: int) -> _FakeLocatorHandle:
            return _RevertingHandle(self._page, self._selector, index)

    class _RevertingPage(_ComposePage):
        def locator(self, selector: str) -> _FakeLocator:  # type: ignore[override]
            return _RevertingLocator(self, selector)

    page = _RevertingPage(SendGate())
    results = _explore(page, monkeypatch)

    scout = [r for r in results if "scout-button" in r.selector]
    assert scout, "現れたバーのボタンまで辿れていない"
    assert not scout[0].sentinel_written, "残らなかった値を書けたことにしている"
    assert scout[0].write_failure_kind == "書いた値が残らなかった"


def test_writing_the_sentinel_never_touches_the_page_outside_the_revealed_region(
    monkeypatch,
) -> None:
    """カードの中の候補は「現れた領域」を持たない。**書き込まない。**

    画面全体に書き込むと、常駐している検索欄まで書き換えて一覧そのものが変わり、
    以降に押しているものが別物になる。
    """
    import jobmedley_scout.recon.capture_open as module

    gate = SendGate()
    page = _ComposePage(gate)
    monkeypatch.setattr(module, "dom_tree", lambda p: p._card_only)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)

    gate.arm()
    try:
        results = explore_card_actions(
            page,
            tree=page._card_only,
            row_index=1,
            sentinel="ZZRECON-TEST",
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
        )
    finally:
        gate.disarm()

    assert page.fills == []
    assert all(not r.sentinel_written for r in results)


# --- 実測5回目: 媒体が GraphQL の単一ページアプリだった --------------------------


def test_the_relaxed_gate_still_stops_the_scout_mutation(monkeypatch) -> None:
    """**緩和しても送信は通らない。**

    媒体は画面を開くための読み取りも POST で送る。全部止めると探索が
    ``/customers/network_error/`` で終わるので、読み取り (GraphQL の ``query``)
    だけを通すようにした。**スカウト送信は ``mutation`` なので通らない。**
    """
    import jobmedley_scout.recon.capture_open as module
    from jobmedley_scout.recon.gate import GateMode

    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    page = _ChainPage(gate)
    graphql = "https://customers.job-medley.com/api/customers/graphql/MemberOnScoutProfile"

    def _click(self, timeout: int = 0) -> None:
        page.clicks.append((self._selector, self._index, page.gate.is_armed))
        if "checkbox" in self._selector:
            page.tree = page._with_bar
            # 画面を開くための読み取り。**通らないと画面が出ない。**
            page.fire_request("POST", graphql, '{"query": "query Profile { member { id } }"}')
        if "scout" in self._selector:
            # スカウト送信。**絶対に通ってはいけない。**
            page.fire_request(
                "POST", graphql, '{"query": "mutation SendScout { sendScout { id } }"}'
            )

    monkeypatch.setattr(_FakeLocatorHandle, "click", _click)
    monkeypatch.setattr(module, "dom_tree", lambda p: p.tree)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)

    gate.arm()
    try:
        results = explore_card_actions(
            page,
            tree=page._card_only,
            row_index=1,
            sentinel="S",
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
        )
    finally:
        gate.disarm()

    # 読み取りは通った (これが無いと画面が開かない)。
    assert page.sent == [graphql]
    assert [e.url for e in gate.passed_reads] == [graphql]
    # **送信は止まった。** 止めた通信が観測として返る。
    scout = [r for r in results if "scout-button" in r.selector]
    assert scout, "現れたバーのボタンまで辿れていない"
    assert scout[0].blocked, "送信が記録されていない"
    assert page.sent.count(graphql) == 1, "**送信まで通ってしまった**"


def test_the_report_says_what_the_gate_let_through() -> None:
    """緩和が黙って効いていると、運用者は何が守られているのか確かめられない。"""
    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(
            AttemptResult(selector="label.c-checkbox", nth=0, looks_like_send=False, clicked=True),
        ),
        reads_allowed=("POST https://customers.job-medley.com/api/customers/graphql/Profile",),
    ).render()

    assert "通した読み取り" in report
    assert "1 件" in report
    assert "graphql/Profile" in report
    # 通した実行で「全て止めた」と書かない。
    assert "武装中の非GETは全て止めています" not in report


def test_a_run_that_let_nothing_through_still_says_it_blocked_everything() -> None:
    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(
            AttemptResult(selector="label.c-checkbox", nth=0, looks_like_send=False, clicked=True),
        ),
    ).render()

    assert "武装中の非GETは全て止めています" in report
    assert "通した読み取り" not in report


# --- 実測7回目: 読み込み表示を「押して現れたもの」として測っていた ---------------


def test_the_loader_does_not_become_the_revealed_region(monkeypatch) -> None:
    """**押した直後に現れるのは読み込み表示である。** その後ろを測る。

    実測6・7回目はどちらも「増えた構造」が div.c-loader 一色で、サイドカバーの
    中身は一度も見えなかった。構造の静止だけでは足りない -- 読み込み中も要素数は
    変わらないので「落ち着いた」と答える。

    だから2回測る。1回目に現れたものの **どれか1つが消える** のを待ってから
    測り直す。消えるのが読み込み表示で、残るのが開いた領域である。
    """
    import jobmedley_scout.recon.capture_open as module

    card_only = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("button", ("c-button", "js-tour-guide-scout-button"), 1),
    )
    loading = _tree(
        ("body", ("c-body", "c-body--fixed-by-sidecover"), -1),
        ("div", ("c-search-member-card",), 0),
        ("button", ("c-button", "js-tour-guide-scout-button"), 1),
        ("div", ("c-loader-view",), 0),
        ("div", ("c-loader",), 3),
    )
    loaded = _tree(
        ("body", ("c-body", "c-body--fixed-by-sidecover"), -1),
        ("div", ("c-search-member-card",), 0),
        ("button", ("c-button", "js-tour-guide-scout-button"), 1),
        ("div", ("c-scout-form",), 0),
        ("textarea", ("c-scout-form__body",), 3),
        ("button", ("c-scout-form__submit",), 3),
    )

    gate = SendGate()
    page = _FakePage(gate, card_only)
    page.tree = card_only

    def _click(self, timeout: int = 0) -> None:
        page.clicks.append((self._selector, self._index, page.gate.is_armed))
        if "scout-button" in self._selector:
            page.tree = loading  # まず読み込み表示が出る

    def _any_detached(p, selectors, timeout_ms):
        # 読み込み表示が消え、中身に差し替わった瞬間。
        if p.tree is loading:
            p.tree = loaded
            return True
        return False

    monkeypatch.setattr(_FakeLocatorHandle, "click", _click)
    monkeypatch.setattr(module, "dom_tree", lambda p: p.tree)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_any_detached", _any_detached)

    gate.arm()
    try:
        results = explore_card_actions(
            page,
            tree=card_only,
            row_index=1,
            sentinel="ZZRECON-TEST",
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
            max_attempts=1,
        )
    finally:
        gate.disarm()

    assert results
    gained = results[0].gained
    # **読み込み表示は「現れたもの」ではない。**
    assert not any("loader" in token for token in gained), f"読み込み表示を測っている: {gained}"
    # その後ろに現れた送信フォームが見えている。
    assert "div.c-scout-form" in gained
    assert "textarea.c-scout-form__body" in gained


def test_the_report_shows_what_vanished_too() -> None:
    """待機していた領域は ``u-is-hidden`` が外れる形で開く = **消える**。

    消えたものを出さないと、その押下は「何も起きなかった」に見える。
    """
    attempt = AttemptResult(
        selector="button.c-button",
        nth=0,
        looks_like_send=False,
        clicked=True,
        lost=("div.u-is-hidden",),
    )
    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(attempt,),
    ).render()

    assert "消えた構造 (1種): div.u-is-hidden" in report


# --- 実測8回目: 開いたのに、その中へ入らず迷子になった --------------------------


def test_the_exploration_enters_what_it_just_opened_first(monkeypatch) -> None:
    """**いま開いたものの中を先に押す (深さ優先)。**

    実測8回目: スカウトのサイドカバーが開いた (150種の新出) のに、次に押したのは
    1つ前に開いたプロフィールモーダルの部品だった。3手のうちに別画面へ遷移して
    探索は終わり、送信フォームには一度も触れていない。

    後ろに足すと「先に開いた領域を全部押してから次へ」になる。導線は
    「開いたものの中へ入っていく」形をしているので、前に足さないと沿えない。
    """
    import jobmedley_scout.recon.capture_open as module

    card = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("button", ("c-open-modal",), 1),
        ("button", ("c-open-cover",), 1),
    )
    with_modal = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("button", ("c-open-modal",), 1),
        ("button", ("c-open-cover",), 1),
        ("div", ("c-modal",), 0),
        ("button", ("c-modal__stray",), 4),
    )
    with_cover = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("button", ("c-open-modal",), 1),
        ("button", ("c-open-cover",), 1),
        ("div", ("c-modal",), 0),
        ("button", ("c-modal__stray",), 4),
        ("div", ("c-side-cover",), 0),
        ("button", ("c-side-cover__next",), 6),
    )

    gate = SendGate()
    page = _FakePage(gate, card)
    page.tree = card

    def _click(self, timeout: int = 0) -> None:
        page.clicks.append((self._selector, self._index, page.gate.is_armed))
        if "c-open-modal" in self._selector:
            page.tree = with_modal
        elif "c-open-cover" in self._selector:
            page.tree = with_cover

    monkeypatch.setattr(_FakeLocatorHandle, "click", _click)
    monkeypatch.setattr(module, "dom_tree", lambda p: p.tree)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_any_detached", lambda *a, **k: True)

    gate.arm()
    try:
        results = explore_card_actions(
            page,
            tree=card,
            row_index=1,
            sentinel="ZZRECON-TEST",
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
            max_attempts=4,
        )
    finally:
        gate.disarm()

    pressed = [r.selector for r in results]
    cover_open = next(i for i, s in enumerate(pressed) if "c-open-cover" in s)
    inside_cover = next((i for i, s in enumerate(pressed) if "c-side-cover__next" in s), None)

    assert inside_cover is not None, "開いたサイドカバーの中に入っていない"
    # **開いた直後に、その中へ入る。** 間に別の領域の部品が挟まらないこと --
    # 挟まると導線から外れ、実測8回目のように別画面へ流れて終わる。
    assert inside_cover == cover_open + 1, f"開いた直後に中へ入っていない: {pressed}"


def test_the_report_separates_no_field_from_could_not_write() -> None:
    """**「無かった」と「在ったが書けなかった」は別の事実である。**"""
    report = OpenObservation(
        requested_url=URL,
        gate_armed=True,
        tree_read=True,
        list_rendered=True,
        rows_found=True,
        attempts=(
            AttemptResult(
                selector="button.c-side-cover__send",
                nth=0,
                looks_like_send=True,
                clicked=True,
                sentinel_written=False,
                text_fields_seen=2,
            ),
        ),
    ).render()

    assert "書き込める部品が 2 個" in report


def test_the_same_region_is_never_followed_twice(monkeypatch) -> None:
    """**チェックボックスの入り切りで、同じ領域を往復し続けない。**

    実測10回目: 入れると ``--checked`` が現れ、外すと ``--scouted`` が現れる。
    交互に「新しい領域が現れた」ことになり、そのたびに同じ候補が積み直された。
    押す対象の重複は防いでいたが、**領域の重複は防いでいなかった。**
    """
    import jobmedley_scout.recon.capture_open as module

    unchecked = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("label", ("c-checkbox",), 1),
        ("div", ("c-card--scouted",), 1),
        ("button", ("c-scouted__x",), 3),
    )
    checked = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("label", ("c-checkbox",), 1),
        ("div", ("c-card--checked",), 1),
        ("button", ("c-checked__y",), 3),
    )

    gate = SendGate()
    page = _FakePage(gate, unchecked)
    page.tree = unchecked

    def _click(self, timeout: int = 0) -> None:
        page.clicks.append((self._selector, self._index, page.gate.is_armed))
        # 押すたびに2つの状態を往復する。
        page.tree = checked if page.tree is unchecked else unchecked

    monkeypatch.setattr(_FakeLocatorHandle, "click", _click)
    monkeypatch.setattr(module, "dom_tree", lambda p: p.tree)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_any_detached", lambda *a, **k: True)

    gate.arm()
    try:
        results = explore_card_actions(
            page,
            tree=unchecked,
            row_index=1,
            sentinel="ZZRECON-TEST",
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
            max_attempts=12,
        )
    finally:
        gate.disarm()

    # 往復し続けるなら上限まで押し続ける。領域の重複を止めていれば早く尽きる。
    assert len(results) < 12, f"同じ領域を往復し続けている: {[r.selector for r in results]}"


def test_closing_is_tried_only_after_the_exploration_finishes(monkeypatch) -> None:
    """**探索の途中で閉じない。** 閉じるとその先が見られなくなる。

    実測11回目: 送信フォームまで到達した直後に閉じる部品を押し、486種の構造が
    一度に消えて実行が終わった。値 (nav.drawer_close_selectors) は要るので、
    **探索が尽きてから** 1回だけ試す。
    """
    import jobmedley_scout.recon.capture_open as module

    card = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("button", ("c-open",), 1),
    )
    opened = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-search-member-card",), 0),
        ("button", ("c-open",), 1),
        ("div", ("c-side-cover",), 0),
        ("a", ("c-side-cover__close-btn",), 3),
        ("button", ("c-side-cover__deep",), 3),
    )

    gate = SendGate()
    page = _FakePage(gate, card)
    page.tree = card
    order: list[str] = []

    def _click(self, timeout: int = 0) -> None:
        order.append(self._selector)
        page.clicks.append((self._selector, self._index, page.gate.is_armed))
        if "c-open" in self._selector:
            page.tree = opened

    monkeypatch.setattr(_FakeLocatorHandle, "click", _click)
    monkeypatch.setattr(module, "dom_tree", lambda p: p.tree)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: None)
    monkeypatch.setattr(module, "wait_for_any_detached", lambda *a, **k: True)

    gate.arm()
    try:
        explore_card_actions(
            page,
            tree=card,
            row_index=1,
            sentinel="ZZRECON-TEST",
            gate=gate,
            config=_Config(),  # type: ignore[arg-type]
            list_url=URL,
            max_attempts=6,
        )
    finally:
        gate.disarm()

    deep = next((i for i, s in enumerate(order) if "deep" in s), None)
    close = next((i for i, s in enumerate(order) if "close-btn" in s), None)

    assert deep is not None, "開いた領域の奥へ入っていない"
    if close is not None:
        assert close > deep, f"探索の途中で閉じている: {order}"
