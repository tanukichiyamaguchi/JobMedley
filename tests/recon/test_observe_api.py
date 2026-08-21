"""一覧を開いて読み取りAPIを聴く。**押さない。値を出さない。**

このコマンドの安全性は2つの約束でできている。

1. **ボタンを1つも押さない** -- 押さなければ送信は起こしようがない (13.6)
2. **応答の値を1つも出さない** -- 一覧の応答は個人データの塊である (13.2)

どちらも「そう書いたつもり」では守れないので、偽のページで確かめる。
"""

from __future__ import annotations

import json

from jobmedley_scout.recon.api_shape import ObservedCall
from jobmedley_scout.recon.gate import GateDecision, GateMode, SendGate
from jobmedley_scout.recon.observe_api import ApiObservation, ApiStage, observe_api

URL = "https://customers.job-medley.com/customers/searches?lg=1"
GRAPHQL = "https://customers.job-medley.com/api/customers/graphql/SearchMembers"

LIST_BODY = json.dumps(
    {
        "data": {
            "searchMembers": {
                "searchUuid": "b1e2c3d4-0000-0000-0000-000000000000",
                "edges": [{"node": {"member": {"id": "00000000", "displayName": "山田太郎"}}}],
            }
        }
    },
    ensure_ascii=False,
)


class _Request:
    def __init__(self, body: str | None, method: str = "POST") -> None:
        self.post_data = body
        self.method = method


class _Response:
    def __init__(
        self, url: str, body: str, request: _Request, content_type: str = "application/json"
    ) -> None:
        self.url = url
        self.request = request
        self._body = body
        self.headers = {"content-type": content_type}

    def text(self) -> str:
        return self._body


class _Page:
    """一覧を開くと GraphQL の応答が届く、最小のページ。"""

    url = URL

    def __init__(self, gate: SendGate) -> None:
        self.gate = gate
        self._handlers: list[object] = []
        self.clicks: list[str] = []
        self.routed = False
        self.responses = [
            _Response(
                GRAPHQL,
                LIST_BODY,
                _Request(json.dumps({"operationName": "SearchMembers"})),
            ),
            # 計測ビーコンの応答。**これは聴かない。**
            _Response(
                "https://www.google-analytics.com/g/collect?v=2",
                '{"ok":1}',
                _Request(None, "POST"),
            ),
        ]

    # --- Playwright が持っている面 -----------------------------------------
    def route(self, pattern: str, handler: object) -> None:
        self.routed = True

    def on(self, event: str, handler: object) -> None:
        if event == "response":
            self._handlers.append(handler)

    def locator(self, selector: str) -> object:
        raise AssertionError("このコマンドは要素に触りません")

    def click(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("このコマンドは押しません")

    # --- 遷移すると応答が流れてくる ----------------------------------------
    def deliver(self) -> None:
        for handler in self._handlers:
            for response in self.responses:
                handler(response)  # type: ignore[operator]


def _install(monkeypatch, page: _Page, *, rendered: bool = True) -> list[SendGate]:
    """偽物を差し込み、**observe_api が自分で作った遮断** を掴んで返す。

    最初この関数は偽ページが持つ遮断を検査していた。それは observe_api が作る
    ものとは **別の物体** なので、武装したかどうかを一度も見ていなかった --
    通っていたのは偽の安心である。実物を掴む。
    """
    import jobmedley_scout.recon.observe_api as module

    captured: list[SendGate] = []

    def _goto(p: _Page, url: str, config: object) -> None:
        p.deliver()

    def _install_gate(p: _Page, g: SendGate) -> None:
        captured.append(g)
        p.route("**/*", None)

    monkeypatch.setattr(module, "goto", _goto)
    monkeypatch.setattr(module, "wait_for_interactive", lambda *a, **k: True)
    monkeypatch.setattr(module, "wait_for_structure_to_settle", lambda *a, **k: True)
    monkeypatch.setattr(module, "login_form_visible", lambda *a, **k: False)
    monkeypatch.setattr(module, "marker_present", lambda *a, **k: rendered)
    monkeypatch.setattr(module, "install_gate", _install_gate)

    class _Ctx:
        def __enter__(self) -> tuple[object, _Page]:
            return object(), page

        def __exit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(module, "browser_context", lambda *a, **k: _Ctx())

    class _Store:
        @staticmethod
        def session_path(_dir: object) -> object:
            class _P:
                @staticmethod
                def exists() -> bool:
                    return True

            return _P()

    monkeypatch.setattr(module, "session_store", _Store)
    return captured


class _Config:
    selector_timeout_ms = 100


def _run(monkeypatch, page: _Page, **kwargs) -> ApiObservation:
    from pathlib import Path

    _install(monkeypatch, page, **kwargs)
    return observe_api(
        _Config(),  # type: ignore[arg-type]
        Path("/nonexistent"),
        URL,
        "div.c-search-member-card",
    )


def test_the_search_identifier_is_found_without_showing_its_value(monkeypatch) -> None:
    """**送信を止めている値の出所が分かる。** そして値は出ない。"""
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    observed = _run(monkeypatch, page)

    assert observed.reached() is ApiStage.HEARD
    assert observed.search_id_candidates() == ("SearchMembers: data.searchMembers.searchUuid",)

    report = observed.render()
    assert "data.searchMembers.searchUuid" in report
    for leaked in ("山田太郎", "00000000", "b1e2c3d4"):
        assert leaked not in report, f"{leaked} が報告に漏れている"


def test_this_command_never_presses_anything(monkeypatch) -> None:
    """**押さなければ送信は起こしようがない** (13.6)。

    偽のページは要素に触られたら例外を投げる。触っていないことが、
    「押さない」の証明になる。
    """
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    _run(monkeypatch, page)
    assert page.clicks == []


def test_the_gate_is_armed_before_the_page_is_opened(monkeypatch) -> None:
    """**武装の位置がこのコマンドの要点である。**

    follow-send は一覧が描画されてから武装するので、一覧そのものの通信が記録に
    入らない。こちらは最初から武装するので、開いた瞬間の読み取りが聴ける。

    検査するのは **observe_api が自分で作った遮断** である。偽ページが持つ別の
    遮断を見ていた時期があり、そのときは何も検査できていなかった。
    """
    import jobmedley_scout.recon.observe_api as module

    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    captured = _install(monkeypatch, page)
    armed_during: list[bool] = []

    def _goto(p: _Page, url: str, config: object) -> None:
        armed_during.append(captured[0].is_armed if captured else False)
        p.deliver()

    monkeypatch.setattr(module, "goto", _goto)
    from pathlib import Path

    observe_api(_Config(), Path("/x"), URL, "div.c-search-member-card")  # type: ignore[arg-type]

    assert captured, "遮断が仕掛けられていない"
    assert armed_during == [True], "一覧を開く時点で武装していない"
    # **書き込みは1つも通らない。** 読み取り (GraphQL の query) だけが通る。
    real = captured[0]
    assert real.mode is GateMode.BLOCK_WRITES
    real.arm()
    try:
        blocked = real.decide("POST", "https://customers.job-medley.com/api/x", "{}")
        passed = real.decide("POST", GRAPHQL, json.dumps({"query": "query SearchMembers { x }"}))
    finally:
        real.disarm()
    assert blocked is not GateDecision.PASS, "書き込みが通った"
    assert passed is GateDecision.PASS, "読み取りまで止めている"


def test_the_gate_is_disarmed_even_if_the_page_blows_up(monkeypatch) -> None:
    """**武装は必ず解除する。** finally で外れることを確かめる (3章)。"""
    import jobmedley_scout.recon.observe_api as module

    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    captured = _install(monkeypatch, page)

    def _boom(p: _Page, url: str, config: object) -> None:
        raise RuntimeError("遷移に失敗")

    monkeypatch.setattr(module, "goto", _boom)
    from pathlib import Path

    import pytest

    with pytest.raises(RuntimeError):
        observe_api(_Config(), Path("/x"), URL, "div.c-search-member-card")  # type: ignore[arg-type]
    assert captured and not captured[0].is_armed, "例外の後も武装したままになっている"


def test_beacons_from_other_origins_are_not_listened_to(monkeypatch) -> None:
    """計測ビーコンの応答まで読むと、報告が他所のサービスの形で埋まる。"""
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    observed = _run(monkeypatch, page)
    assert len(observed.calls) == 1
    assert "google-analytics" not in observed.render()


def test_hearing_nothing_is_reported_as_such(monkeypatch) -> None:
    """**「聴けなかった」を「応答が無かった」にしない** (原則2)。"""
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    page.responses = []
    observed = _run(monkeypatch, page)
    assert observed.reached() is ApiStage.NOTHING_HEARD
    assert "1つも聴けませんでした" in observed.render()


def test_a_list_that_never_rendered_still_reports_what_it_heard(monkeypatch) -> None:
    """**描画と聴取は独立している。**

    最初この2つを1本の鎖に並べ、偽のページで即座に落ちた。応答は遷移の最中に
    届くので、行が現れる前に聴き終わっていることがある。検索が0件でも応答は
    届く。独立した事実を鎖に並べると、**正常な実行が「嘘」として例外になる。**

    描画しなかった事実は捨てず、報告に併記する。
    """
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    observed = _run(monkeypatch, page, rendered=False)
    assert observed.reached() is ApiStage.HEARD, "聴けたのに聴けなかったことにしている"
    report = observed.render()
    assert "一覧の行は現れませんでした" in report
    assert "data.searchMembers.searchUuid" in report


def test_the_report_refuses_to_pick_the_list_endpoint_for_the_operator(monkeypatch) -> None:
    """**どれが一覧の取得かは、値を見ないと決められない。**

    決められないものを機械が1つに決めると、それは推測で座標を埋めることになる
    (原則3)。候補を並べて、選ぶのは人間に委ねる。
    """
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    report = _run(monkeypatch, page).render()
    assert "api.candidate_list.url_pattern: UNRESOLVED" in report
    assert "どれか1つを機械が選ぶことはしません" in report


def test_a_broken_chain_raises_rather_than_reporting_a_lie() -> None:
    """後の工程の証拠が立っているのに前の工程が False なら、**報告せず止まる。**"""
    import pytest

    broken = ApiObservation(
        requested_url=URL,
        list_rendered=False,
        calls=(),
        session_present=False,
    )
    # セッション無しで止まっているので、ここは矛盾しない。
    assert broken.reached() is ApiStage.NO_SESSION

    lying = ApiObservation(
        requested_url=URL,
        session_present=False,
        calls=(ObservedCall(operation="X", redacted_url="u", method="POST"),),
    )
    with pytest.raises(ValueError):
        lying.reached()


def test_writes_during_the_list_load_are_reported_even_when_there_are_none(monkeypatch) -> None:
    """**0件でも0件と書く** (原則2)。

    これまでどのコマンドも、一覧のロード中は武装していなかった (描画を殺さない
    ため、押す直前に武装する設計)。つまり「一覧を開くだけでは書き込みが飛ばない」
    は **一度も観測されていない**。最初から武装するこのコマンドで初めて言える。

    黙ると「観測しなかった」と区別が付かない。
    """
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    report = _run(monkeypatch, page).render()
    assert "書き込み**: 0 件" in report
    assert "開くだけでは何も書き込まれない" in report


def test_a_write_during_the_list_load_is_named_with_its_url_masked(monkeypatch) -> None:
    """飛んでいたら、**伏せたURLで** 名指しする。"""
    import jobmedley_scout.recon.observe_api as module

    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    captured = _install(monkeypatch, page)

    def _goto(p: _Page, url: str, config: object) -> None:
        # 媒体が既読化のPOSTを飛ばす。遮断が止めて記録する。
        captured[0].decide(
            "POST", "https://customers.job-medley.com/api/customers/members/48211/mark_read", "{}"
        )
        p.deliver()

    monkeypatch.setattr(module, "goto", _goto)
    from pathlib import Path

    observed = observe_api(_Config(), Path("/x"), URL, "div.c-search-member-card")  # type: ignore[arg-type]
    report = observed.render()
    assert "書き込み**: 1 件" in report
    assert "mark_read" in report
    assert "48211" not in report, "会員IDが伏せられていない"


# --- 実測21回目: 一覧はサーバ側で組み立てられていた -----------------------------

SERVER_RENDERED = (
    "<html><body>"
    '<div data-search-uuid="b1e2c3d4-0000-0000-0000-000000000000">'
    '<script>window.__DATA__={"searchUuid":"b1e2c3d4-0000-0000-0000-000000000000",'
    '"memberId":3323741}</script>'
    "山田太郎</body></html>"
)


def test_a_server_rendered_list_is_heard_at_all(monkeypatch) -> None:
    """**GraphQL だけを聴いていたら、一覧は永久に聴こえない。**

    実測21回目、一覧は描画されたのに聴けた応答は0件だった。送信が GraphQL だった
    ので読み取りもそうだろうと考えて絞り込んでいたが、一覧のURLは
    ``/customers/searches?lg=0&...`` という問い合わせつきのページで、
    **サーバ側で組み立てられて返ってくる**。GraphQL は1本も飛ばない。
    """
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    page.responses = [
        _Response(
            "https://customers.job-medley.com/customers/searches?lg=0",
            SERVER_RENDERED,
            _Request(None, "GET"),
            content_type="text/html; charset=utf-8",
        ),
    ]
    observed = _run(monkeypatch, page)
    assert observed.reached() is ApiStage.HEARD, "サーバ描画の一覧が聴けていない"
    assert observed.calls[0].content_type == "text/html"


def test_a_non_json_document_is_measured_without_reading_its_values(monkeypatch) -> None:
    """**値は取り出さない。数だけ数える。**

    JSON でなければキーパスは辿れない。それでも「送信に要る値がこの文書に
    入っているのか」は、UUIDの形の数と **キーの名前** の出現数で分かる。
    数は個人データではない。
    """
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    page.responses = [
        _Response(
            "https://customers.job-medley.com/customers/searches?lg=0",
            SERVER_RENDERED,
            _Request(None, "GET"),
            content_type="text/html",
        ),
    ]
    report = _run(monkeypatch, page).render()
    assert "UUIDの形が 1 個" in report
    assert "送信キーの名前が" in report
    for leaked in ("b1e2c3d4", "山田太郎", "3323741"):
        assert leaked not in report, f"{leaked} が報告に漏れている"


def test_hearing_nothing_reports_the_numbers_that_make_it_diagnosable(monkeypatch) -> None:
    """**「無かった」と「見ていなかった」を、報告だけで切り分けられること。**

    実測21回目、聴けた応答は0件だったが「いくつ無視したか」を出していなかった。
    そのため「本当に応答が無かった」のか「絞り込みが狭すぎた」のかが報告からは
    決められなかった -- **自分で作った静かなゼロ件である**。
    """
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    page.responses = [
        _Response(
            "https://www.google-analytics.com/g/collect",
            "{}",
            _Request(None, "POST"),
        ),
    ]
    observed = _run(monkeypatch, page)
    assert observed.reached() is ApiStage.NOTHING_HEARD
    report = observed.render()
    assert "聴く仕掛け: 張れました" in report
    assert "聴かなかった応答: 1 件" in report
    assert "媒体だけが無言でした" in report
    # 書き込みの件数は、聴けなかった実行でも出す。
    assert "書き込み**: 0 件" in report


def test_a_listener_that_never_attached_says_so(monkeypatch) -> None:
    """**張れなかったことを黙らない。** 応答の有無以前の問題である。"""
    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))

    def _refuse(self: _Page, event: str, handler: object) -> None:
        raise RuntimeError("張れません")

    monkeypatch.setattr(_Page, "on", _refuse)
    observed = _run(monkeypatch, page)
    assert observed.reached() is ApiStage.NOTHING_HEARD
    report = observed.render()
    assert "張れませんでした" in report
    assert "応答の有無以前の問題" in report


def test_binary_responses_are_counted_but_not_read(monkeypatch) -> None:
    """画像は読む意味が無く、重い。**数だけ残す。**"""

    class _Exploding(_Response):
        def text(self) -> str:
            raise AssertionError("画像の本文を読んでいる")

    page = _Page(SendGate(mode=GateMode.BLOCK_WRITES))
    page.responses = [
        _Exploding(
            "https://customers.job-medley.com/assets/logo.png",
            "",
            _Request(None, "GET"),
            content_type="image/png",
        ),
        _Response(
            "https://customers.job-medley.com/customers/searches",
            SERVER_RENDERED,
            _Request(None, "GET"),
            content_type="text/html",
        ),
    ]
    observed = _run(monkeypatch, page)
    assert len(observed.calls) == 1
    assert observed.skipped_binary == 1
    assert "本文を見なかった応答: 1 件" in observed.render()
