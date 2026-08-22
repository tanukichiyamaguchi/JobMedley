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
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
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
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
    _run(monkeypatch, page)
    assert page.clicks == []


def test_the_media_search_post_is_not_blocked(monkeypatch) -> None:
    """**観測したかったものを、観測のための仕掛けが止めてはいけない。**

    実測22回目、書き込みを全部止めたまま一覧を開いたら、候補者を取ってくる通信
    そのものが止まった::

        POST /api/customers/customer_search_conditions/search_manual/
        POST /api/customers/received_favorites/search/
        POST /api/customers/scouted_members/search/

    **この媒体の読み取りは GraphQL ではなく REST の POST である。** 遮断から見れば
    書き込みと区別が付かない。だから媒体のオリジンは素通しにする。

    安全性は遮断ではなく **押さないこと** で担保する
    (``tests/guardrails/test_observe_only_never_presses.py``)。

    検査するのは **observe_api が自分で作った遮断** である。偽ページが持つ別の
    遮断を見ていた時期があり、そのときは何も検査できていなかった。
    """
    import jobmedley_scout.recon.observe_api as module

    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
    captured = _install(monkeypatch, page)
    armed_during: list[bool] = []

    def _goto(p: _Page, url: str, config: object) -> None:
        armed_during.append(captured[0].is_armed if captured else False)
        p.deliver()

    monkeypatch.setattr(module, "goto", _goto)
    from pathlib import Path

    observe_api(_Config(), Path("/x"), URL, "div.c-search-member-card")  # type: ignore[arg-type]

    assert captured, "遮断が仕掛けられていない"
    assert armed_during == [True], "一覧を開く時点で仕掛けが効いていない"

    real = captured[0]
    assert real.mode is GateMode.BLOCK_THIRD_PARTY
    real.arm()
    try:
        search = real.decide(
            "POST",
            "https://customers.job-medley.com/api/customers/received_favorites/search/",
            "{}",
        )
        beacon = real.decide("POST", "https://www.google-analytics.com/g/collect", "{}")
    finally:
        real.disarm()
    assert search is GateDecision.PASS, "候補者を取ってくる通信を止めている"
    assert beacon is not GateDecision.PASS, "計測ビーコンまで通している"


def test_the_gate_is_disarmed_even_if_the_page_blows_up(monkeypatch) -> None:
    """**武装は必ず解除する。** finally で外れることを確かめる (3章)。"""
    import jobmedley_scout.recon.observe_api as module

    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
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
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
    observed = _run(monkeypatch, page)
    assert len(observed.calls) == 1
    assert "google-analytics" not in observed.render()


def test_hearing_nothing_is_reported_as_such(monkeypatch) -> None:
    """**「聴けなかった」を「応答が無かった」にしない** (原則2)。"""
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
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
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
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
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
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


def test_both_what_flew_and_what_was_stopped_are_reported(monkeypatch) -> None:
    """**0件でも0件と書く** (原則2)。そして **飛んだ側と止めた側を分ける。**

    媒体へ飛んだ非GET には候補者一覧の取得が含まれる (座標の本命)。他所へ行こう
    として止めたものは計測ビーコンで、観測には要らない。混ぜて数えると本命が
    雑音に埋もれる -- 実測22回目は534件のうち529件が計測ビーコンだった。
    """
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
    report = _run(monkeypatch, page).render()
    assert "媒体のオリジンへ飛んだ非GET: 0 件" in report
    assert "止めた** 通信: 0 件" in report


def test_a_media_post_that_flew_is_named_with_its_url_masked(monkeypatch) -> None:
    """飛んだものも、**伏せたURLで** 名指しする。ここに一覧の取得が居る。"""
    import jobmedley_scout.recon.observe_api as module

    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
    captured = _install(monkeypatch, page)

    def _goto(p: _Page, url: str, config: object) -> None:
        # 媒体が候補者を取ってくる。**止めない。** 通した事実を記録する。
        captured[0].decide(
            "POST", "https://customers.job-medley.com/api/customers/members/48211/search/", "{}"
        )
        p.deliver()

    monkeypatch.setattr(module, "goto", _goto)
    from pathlib import Path

    observed = observe_api(_Config(), Path("/x"), URL, "div.c-search-member-card")  # type: ignore[arg-type]
    report = observed.render()
    assert "媒体のオリジンへ飛んだ非GET: 1 件" in report
    assert "search/" in report
    assert "48211" not in report, "会員IDが伏せられていない"


def test_the_same_endpoint_is_not_listed_ninety_times(monkeypatch) -> None:
    """**同じ行を何十回も出す報告は、出していないのと大差ない。**

    実測22回目、単一ページアプリが同じ4本を取り直したせいで、座標の候補一覧が
    同じURLで90行埋まった。読む人が本命を見つけられない。
    """
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
    same = _Response(
        "https://customers.job-medley.com/api/customers/version/",
        '{"version":"1"}',
        _Request(None, "GET"),
    )
    page.responses = [same] * 30
    report = _run(monkeypatch, page).render()
    assert report.count("api/customers/version/") <= 3, "同じURLが何度も並んでいる"


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
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
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
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
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
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
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
    # 飛んだ/止めたの件数は、聴けなかった実行でも出す。
    assert "媒体のオリジンへ飛んだ非GET: 0 件" in report


def test_a_listener_that_never_attached_says_so(monkeypatch) -> None:
    """**張れなかったことを黙らない。** 応答の有無以前の問題である。"""
    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))

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

    page = _Page(SendGate(mode=GateMode.BLOCK_THIRD_PARTY))
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


# ===========================================================================
# 実測23回目で分かった2つの穴
# ===========================================================================

#: 実測した候補者一覧の応答の形 (キー名だけ本物、値は捨て値)。
REST_LIST_BODY = json.dumps(
    {
        "members": [{"id": 1, "code": "M-1", "age": "20代", "scouted": False}],
        "search_uuid": "b1e2c3d4-0000-0000-0000-000000000000",
        "total": 1,
        "page": 1,
        "limit": 25,
        "next_cursor": "zzz",
    }
)

#: 一覧を要求する本文。**検索条件が載る。値は報告に出してはいけない** (13.2)。
REST_LIST_REQUEST = json.dumps(
    {"pagination": {"page": 1, "limit": 25}, "age": {"from": 20, "to": 40}}
)

REST_LIST_URL = "https://customers.job-medley.com/api/customers/members/search/"

#: 実測23回目に素通ししてしまったビーコン。**クエリに媒体のホスト名が入る。**
BEACON_NAMING_THE_MEDIA = (
    "https://www.google-analytics.com/g/collect?v=2&tid=G-X"
    "&dl=https%3A%2F%2Fcustomers.job-medley.com%2Fcustomers%2Fsearches"
)


class _RestPage(_Page):
    """一覧が **REST の POST** で来るページ。実測どおりの作り。"""

    def __init__(self, gate: SendGate) -> None:
        super().__init__(gate)
        self.responses = [
            _Response(REST_LIST_URL, REST_LIST_BODY, _Request(REST_LIST_REQUEST, "POST")),
            _Response(BEACON_NAMING_THE_MEDIA, '{"ok":1}', _Request("{}", "POST")),
        ]


def test_a_beacon_that_names_the_media_in_its_query_is_still_ignored(monkeypatch) -> None:
    """**URL全体の部分一致では、計測ビーコンが媒体の通信に化ける。**

    実測23回目の報告は、Google のビーコンを「媒体のオリジンへ飛んだ非GET」
    として並べていた。ビーコンは送信元ページのURLを ``dl=`` に載せるので、
    URLの文字列の中には媒体のホスト名がそのまま入っている。

    既存の試験がこれを捕まえられなかったのは、偽のビーコンURLが素っ気なさ
    すぎて ``dl=`` を持っていなかったからである -- **偽物が本物より
    行儀が良いと、試験は穴を通す。**
    """
    observed = _run(monkeypatch, _RestPage(SendGate(mode=GateMode.BLOCK_THIRD_PARTY)))
    assert len(observed.calls) == 1, "計測ビーコンを媒体の応答として聴いています"
    assert observed.ignored == 1
    assert "google-analytics" not in observed.render()


def test_a_rest_call_is_named_so_the_report_can_be_read(monkeypatch) -> None:
    """実測23回目の報告は、19本すべてが「(名前を読めませんでした)」だった。"""
    observed = _run(monkeypatch, _RestPage(SendGate(mode=GateMode.BLOCK_THIRD_PARTY)))
    report = observed.render()
    assert "操作: members/search" in report
    assert "名前を読めませんでした" not in report


def test_the_request_shape_is_reported_so_the_endpoint_can_be_called(monkeypatch) -> None:
    """**URLが分かっても、送る中身が分からなければ呼べない。**

    3回目の observe-api は応答しか出していなかったので、``members/search`` の
    URLは決まったのに「何を送れば同じ並びが返るか」は分からないままだった。
    """
    observed = _run(monkeypatch, _RestPage(SendGate(mode=GateMode.BLOCK_THIRD_PARTY)))
    report = observed.render()
    assert "**要求本文** のキー" in report
    assert "pagination.limit: <number>" in report
    assert "age.from: <number>" in report


def test_the_request_shape_never_carries_the_search_values(monkeypatch) -> None:
    """要求本文には検索条件が載る。**そこから個人が絞り込まれうる** (13.2)。"""
    observed = _run(monkeypatch, _RestPage(SendGate(mode=GateMode.BLOCK_THIRD_PARTY)))
    report = observed.render()
    for leaked in (": 25", ": 20", ": 40", "M-1", "b1e2c3d4"):
        assert leaked not in report, f"{leaked} が報告に漏れている"


def test_the_search_identifier_is_found_in_the_rest_response(monkeypatch) -> None:
    """``search_uuid`` は **候補者一覧と同じ応答に載っている**。"""
    observed = _run(monkeypatch, _RestPage(SendGate(mode=GateMode.BLOCK_THIRD_PARTY)))
    assert observed.search_id_candidates() == ("members/search: search_uuid",)
