"""次の実行で起こりうる画面の形を、実行前に列挙して通す。

**運用者の往復は1回ごとに手間である。** これまで「実行 → 想定外の構造で失敗 →
修正 → 再実行」を3回繰り返した。このファイルはその回り方を変えるためにある --
起こりうる形を先にここへ並べ、:func:`analyze_candidate_list` (実行時と再生の
両方が通る唯一の解析関数) が全形で **正しい値か、理由付きの UNRESOLVED** を
返すことを、実行前に固定する。

新しい形が実機で見つかったら、まずここに fixture として追加し、手元で直してから
次の実行を頼む。**このファイルが増えることが、往復が減ることである。**

不変条件 (全variantに共通):
  * 枠 (``body.c-body`` 等、両ページに存在するトークン) は値にも別案にも出ない
  * 値が出ないときは、出ない理由が報告に含まれる
  * 出力キーは常に段階2の4個ちょうど
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from jobmedley_scout.browser.dom import DomNode, DomTree
from jobmedley_scout.recon.observe_list import STAGE_2_KEYS, ObservedList, analyze_candidate_list
from jobmedley_scout.recon.snapshot import ListCapture, ZeroCapture

URL = (
    "https://customers.job-medley.com/customers/searches?age[from]=0&age[to]=40&pagination[page]=1"
)


def _tree(*rows: tuple[str, tuple[str, ...], int]) -> DomTree:
    return DomTree(
        nodes=tuple(DomNode(tag=t, class_names=c, parent=p) for t, c, p in rows),
        truncated=False,
        shadow_root_count=0,
    )


def _card(parent: int, base: int, scouted: bool = False) -> list[tuple[str, tuple[str, ...], int]]:
    """実測のカード構造の縮約。文字要素×2 + スカウトボタン。"""
    classes = (
        ("c-search-member-card", "c-search-member-card--scouted")
        if scouted
        else ("c-search-member-card",)
    )
    return [
        ("div", classes, parent),  # base
        ("div", ("c-search-member-card__field",), base),  # base+1
        ("p", ("c-search-member-card-text",), base + 1),  # base+2
        ("p", ("c-search-member-card-text",), base + 1),  # base+3
        ("button", ("js-tour-guide-scout-button",), base),  # base+4
    ]


def _results_page(cards: int = 3, scouted_at: frozenset[int] = frozenset({1})) -> DomTree:
    """実測の形: body > 2つの c-segment、片方が一覧 (無限スクロール枠) を持つ。"""
    rows: list[tuple[str, tuple[str, ...], int]] = [
        ("body", ("c-body",), -1),  # 0
        ("div", ("c-segment",), 0),  # 1 検索条件パネル
        ("p", ("c-filter-text",), 1),  # 2
        ("div", ("c-segment",), 0),  # 3 一覧の区画
        ("div", ("js-infinity-scroll-outer-el",), 3),  # 4
    ]
    for i in range(cards):
        rows.extend(_card(4, len(rows), scouted=i in scouted_at))
    return _tree(*rows)


def _frame_only(*extra: tuple[str, tuple[str, ...], int]) -> DomTree:
    """枠 + 追加要素。0件ページの土台。"""
    rows: list[tuple[str, tuple[str, ...], int]] = [
        ("body", ("c-body",), -1),
        ("div", ("c-segment",), 0),
        ("p", ("c-filter-text",), 1),
        ("div", ("c-segment",), 0),
        ("div", ("js-infinity-scroll-outer-el",), 3),
    ]
    rows.extend(extra)
    return _tree(*rows)


EMPTY_STATE = ("div", ("c-search-empty",), 4)
LOADING = ("div", ("c-loading",), 4)


def _zero(kind: str, settled: DomTree | None, early: DomTree | None = None) -> ZeroCapture:
    return ZeroCapture(kind=kind, url=URL, landed_url=URL, early=early, settled=settled)


def _analyze(results: DomTree | None, *zeros: ZeroCapture) -> ObservedList:
    return analyze_candidate_list(
        ListCapture(requested_url=URL, landed_url=URL, results=results, zeros=tuple(zeros))
    )


FRAME_TOKENS = ("body.c-body", "div.c-segment", "div.js-infinity-scroll-outer-el")


def _assert_no_frame_in_values(observed: ObservedList) -> None:
    for value in observed.ready:
        for token in FRAME_TOKENS:
            assert token != value.row_token and token != value.empty_token


# --- variant: 理想形 -----------------------------------------------------------


def test_ideal_two_clean_zero_pages() -> None:
    """両方の0件変種が本物の0件表示を返す、いちばん素直な形。"""
    zero_page = _frame_only(EMPTY_STATE)
    observed = _analyze(_results_page(), _zero("age", zero_page), _zero("pagination", zero_page))

    assert observed.ready
    assert observed.ready[0].selector() == "div.c-search-member-card, div.c-search-empty"
    assert observed.rows_confirmed_vanishing is True
    assert observed.empty_state_single_variant is False
    _assert_no_frame_in_values(observed)


# --- variant: 実測1回目の形 (0件比較が全滅) --------------------------------------


def test_both_zero_pages_still_show_cards() -> None:
    """検索条件が差し戻され、0件のはずのページにカードが並ぶ (実測2回目の型)。"""
    contaminated = _results_page(cards=2, scouted_at=frozenset())
    observed = _analyze(
        _results_page(), _zero("age", contaminated), _zero("pagination", contaminated)
    )

    # 値は出ない。枠も出ない。理由は「1つも消えていません」。
    assert observed.ready == ()
    report = observed.render()
    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "1つも消えていません" in report
    assert "そのまま座標に書かないでください" in report


def test_one_clean_one_contaminated() -> None:
    """片方だけ本物。**悪い方に引きずられず、良い方だけで値を出す。** ただし注記付き。"""
    observed = _analyze(
        _results_page(),
        _zero("age", _results_page(cards=2, scouted_at=frozenset())),  # 差し戻し
        _zero("pagination", _frame_only(EMPTY_STATE)),  # 本物
    )

    assert observed.ready
    assert observed.ready[0].empty_token == "div.c-search-empty"
    assert observed.empty_state_single_variant is True
    assert "1種類しか使えませんでした" in observed.render()
    _assert_no_frame_in_values(observed)


# --- variant: 未描画・読み込み表示 ----------------------------------------------


def test_loading_skeleton_on_the_zero_page_is_not_the_empty_state() -> None:
    """0件ページの読み込み表示は、遷移直後の1枚 (early) に写っているので外れる。"""
    early = _frame_only(LOADING)
    settled = _frame_only(LOADING, EMPTY_STATE)
    observed = _analyze(
        _results_page(), _zero("age", settled, early), _zero("pagination", settled, early)
    )

    assert observed.ready
    assert observed.ready[0].empty_token == "div.c-search-empty"
    assert all(v.empty_token != "div.c-loading" for v in observed.ready)


def test_a_zero_page_captured_before_anything_rendered() -> None:
    """settled まで骨組みのまま = 0件表示が結局現れない。値は出ない (見える失敗)。"""
    skeleton = _frame_only(LOADING)
    observed = _analyze(
        _results_page(), _zero("age", skeleton, skeleton), _zero("pagination", skeleton, skeleton)
    )

    assert observed.ready == ()
    assert "nav.list_ready_selector: UNRESOLVED" in observed.render()


def test_one_non_rendered_variant_does_not_veto_the_one_that_loaded() -> None:
    """**実測7回目の形。** age は本物の0件を返したが、pagination は起動前スケルトンの
    まま撮影された (別URLへ転送された可能性。サイトの共通要素をほとんど含まない)。

    旧実装ではスケルトンの「繰り返し全滅」が使用可能と誤判定され、``ready_values``
    が「0件表示は全使用可能0件ページに存在」を要求するために age の
    ``div.c-search-empty`` を全否定して UNRESOLVED になった。スケルトンを弾けば、
    ちゃんと描画された age 側から値が出る (1種のみの注記付き)。
    """
    loaded = _frame_only(EMPTY_STATE)  # age: 共通要素を保持した本物の0件
    skeleton = _tree(("body", ("c-skeleton",), -1), ("div", ("c-boot",), 0))  # 未描画

    observed = _analyze(_results_page(), _zero("age", loaded), _zero("pagination", skeleton))

    assert observed.ready
    assert observed.ready[0].empty_token == "div.c-search-empty"
    report = observed.render()
    assert "共通要素" in report  # スケルトンは非ロードとして診断
    assert "1種類しか使えませんでした" in report  # 1変種のみの注記


# --- variant: 0件ページが読めない・木が壊れる ------------------------------------


def test_unreadable_zero_pages_refuse_honestly() -> None:
    observed = _analyze(_results_page(), _zero("age", None), _zero("pagination", None))

    assert observed.ready == ()
    assert "要素が無かったのではありません" in observed.render()


def test_unreadable_results_page() -> None:
    observed = _analyze(None)

    report = observed.render()
    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "DOMの木を読めませんでした" in report


def test_zero_page_redirected_to_sign_in() -> None:
    """0件変種の途中でセッションが切れた形。転送の検査で捕まる。"""
    observed = _analyze(
        _results_page(),
        ZeroCapture(
            kind="age",
            url=URL,
            landed_url="https://customers.job-medley.com/customers/sign_in/",
            early=None,
            settled=_frame_only(),
        ),
    )

    assert observed.ready == ()
    assert "転送" in observed.render()


# --- variant: 別のレイアウト ----------------------------------------------------


def test_a_flat_layout_without_segments() -> None:
    """区画の無い素直なレイアウトでも同じ答えになること (形への依存を作らない)。"""
    results = _tree(
        ("body", ("c-body",), -1),
        ("main", ("o-main",), 0),
        ("ul", ("member-list",), 1),
        ("li", ("member",), 2),
        ("li", ("member",), 2),
        ("li", ("member",), 2),
    )
    zero = _tree(
        ("body", ("c-body",), -1),
        ("main", ("o-main",), 0),
        ("ul", ("member-list",), 1),
        ("div", ("no-results",), 2),
    )
    observed = _analyze(results, _zero("age", zero), _zero("pagination", zero))

    assert observed.ready
    assert observed.ready[0].selector() == "li.member, div.no-results"


def test_full_markup_recommendation_cards_refuse_the_zero_page() -> None:
    """0件ページに「おすすめ」が **完全なカード** で出る形。

    カードも文字要素も残る = 結果ページの繰り返しが1つも消えない = 拒否。
    """
    zero_with_recos = _frame_only(
        ("div", ("c-recommend",), 4),
        ("div", ("c-search-member-card",), 5),
        ("p", ("c-search-member-card-text",), 6),
        ("p", ("c-search-member-card-text",), 6),
        ("div", ("c-search-member-card",), 5),
        ("p", ("c-search-member-card-text",), 9),
        ("p", ("c-search-member-card-text",), 9),
        EMPTY_STATE,
    )
    observed = _analyze(
        _results_page(), _zero("age", zero_with_recos), _zero("pagination", zero_with_recos)
    )

    assert observed.ready == ()
    assert "1つも消えていません" in observed.render()


def test_bare_shell_recommendation_cards_slide_the_row_inward_but_never_to_a_frame() -> None:
    """0件ページに「おすすめ」が **骨だけのカード** で出る形 (反証が突いた最悪ケース)。

    文字要素は消えているのでページは受理され、カードは残っているので行候補から
    外れ、行は内側の文字要素へ滑る。**それでも枠は出ない** -- 滑った先も
    「内容が出たら一致する」トークンであり、常真にはならない。診断 (残存の内訳) が
    報告に出るので、この状態は実行ログとスナップショットから見抜ける。
    """
    zero_with_shells = _frame_only(
        ("div", ("c-recommend",), 4),
        ("div", ("c-search-member-card",), 5),
        ("div", ("c-search-member-card",), 5),
        EMPTY_STATE,
    )
    observed = _analyze(
        _results_page(), _zero("age", zero_with_shells), _zero("pagination", zero_with_shells)
    )

    # 行は内側へ滑る。値は出るが、枠は決して出ない。
    assert observed.ready
    assert observed.ready[0].row_token == "p.c-search-member-card-text"
    _assert_no_frame_in_values(observed)
    # 診断が残る。
    assert "残存" in observed.render()


# --- 全variant共通の不変条件 -----------------------------------------------------


@pytest.mark.parametrize(
    "observed",
    [
        _analyze(_results_page(), _zero("age", _frame_only(EMPTY_STATE))),
        _analyze(_results_page()),
        _analyze(None),
        _analyze(
            _results_page(cards=2, scouted_at=frozenset()),
            _zero("age", _results_page(cards=2, scouted_at=frozenset())),
        ),
    ],
)
def test_every_variant_emits_exactly_the_stage_2_keys(observed: ObservedList) -> None:
    parsed = yaml.safe_load(textwrap.dedent(observed.yaml_block()))

    assert set(parsed) == set(STAGE_2_KEYS)


def test_replay_equals_live_analysis() -> None:
    """**再生と実行が同じ答えを出すこと。** 再生の存在意義そのもの。

    スナップショットの往復 (保存形式 → 読み戻し) を挟んでも、解析結果の
    レポートが1文字も変わらないことを固定する (ドロワーの注記だけが再生固有)。
    """
    from jobmedley_scout.recon.snapshot import capture_from_payload, capture_to_payload

    capture = ListCapture(
        requested_url=URL,
        landed_url=URL,
        results=_results_page(),
        zeros=(
            _zero("age", _frame_only(EMPTY_STATE)),
            _zero("pagination", _frame_only(EMPTY_STATE)),
        ),
    )
    reloaded = capture_from_payload(capture_to_payload(capture))
    assert reloaded is not None

    assert analyze_candidate_list(reloaded).render() == analyze_candidate_list(capture).render()


# --- variant: 実測3回目で判明した形 (ローダーが残る / コンテナが観測できる) --------


def test_a_stuck_loader_zero_page_is_visible_in_the_diagnostics() -> None:
    """**実測3回目の形。** 0件変種の読み込みが完了せず、ローダーだけが残った。

    値は出ない (0件表示を観測できていない) が、``loader_cleared=False`` の事実が
    診断として必ず印字されること。これが無いと「0件表示が無い媒体」と
    「読み込みが終わらなかった実行」を、報告から区別できない。

    文言は「読み込み表示が残った」と **断定しない** -- 実測4回目で、残っていたのは
    先に描画された0件表示で、ローダーは剥がれていた (待ちの対象が誤っていただけ)。
    """
    skeleton = _frame_only(LOADING)
    observed = _analyze(
        _results_page(),
        ZeroCapture(
            kind="age",
            url=URL,
            landed_url=URL,
            early=skeleton,
            settled=skeleton,
            loader_cleared=False,
        ),
    )

    assert observed.ready == ()
    report = observed.render()
    assert "遷移直後からの要素が一部残りました" in report


def test_a_post_load_marker_when_the_medium_has_no_empty_element() -> None:
    """0件表示の専用要素が無い媒体の形 -- 一覧が空のコンテナだけを残して消える。

    ペアは原理的に組めないので、「検索応答の描画後にのみ現れる要素」単独が
    値になる -- 件数に依らず描画完了を待てる。
    """
    # 結果ページ: 一意なコンテナ div.result-body の中に行が並ぶ。
    results = _tree(
        ("body", ("c-body",), -1),
        ("div", ("result-body",), 0),  # アンカー
        ("div", ("card",), 1),
        ("p", ("t",), 2),
        ("div", ("card",), 1),
        ("p", ("t",), 4),
    )
    # 0件ページ (settled): コンテナは残るが、中身も0件表示も無い。
    zero_settled = _tree(
        ("body", ("c-body",), -1),
        ("div", ("result-body",), 0),
    )
    # 遷移直後: コンテナ自体がまだ無い (= 出現を待てる)。ローダーは後で剥がれる。
    zero_early = _tree(
        ("body", ("c-body",), -1),
        ("div", ("c-loader",), 0),
    )
    observed = _analyze(
        results,
        ZeroCapture(
            kind="age",
            url=URL,
            landed_url=URL,
            early=zero_early,
            settled=zero_settled,
            loader_cleared=True,
        ),
        ZeroCapture(
            kind="pagination",
            url=URL,
            landed_url=URL,
            early=zero_early,
            settled=zero_settled,
            loader_cleared=True,
        ),
    )

    assert observed.ready == ()
    assert observed.loaded_marker == "div.result-body"
    report = observed.render()
    assert 'nav.list_ready_selector: "div.result-body"' in report
    assert "検索応答の描画後にのみ現れる要素" in report


def test_a_zero_display_prerendered_at_the_early_snapshot_still_pairs() -> None:
    """**実測4回目の形。** SPA が速く、age 変種の0件表示 (c-not-found) は遷移直後の
    1枚に写り終わっていた。ローダーはその後に剥がれた。pagination 変種の直後の
    1枚は起動前の骨組みで、settled はローダーが残ったまま (読み込み途中) だが、
    0件表示は既に描画されていた。

    「early に在る」を理由に0件表示を捨てると、実在する専用要素が UNRESOLVED に
    なる (3回目の「この媒体に0件表示は無い」はこの取り違えから生まれた誤り)。
    除外してよいのは「消えたことが観測された」ものだけである。
    """
    # 実際の媒体の形: 0件ページでは無限スクロール枠 (アンカー) ごと消える。
    zero_frame: list[tuple[str, tuple[str, ...], int]] = [
        ("body", ("c-body",), -1),
        ("div", ("c-segment",), 0),
        ("p", ("c-filter-text",), 1),
        ("div", ("c-segment",), 0),
    ]
    zero_display = ("div", ("c-not-found", "c-not-found--searches"), 3)
    loader = ("div", ("c-loader-view",), 3)
    age_early = _tree(*zero_frame, loader, zero_display)  # ローダー + 0件表示が同居
    age_settled = _tree(*zero_frame, zero_display)  # ローダーだけが剥がれた
    thin_early = _tree(("body", ("c-body",), -1))  # 起動前の骨組み
    pagination_settled = _tree(*zero_frame, loader, zero_display)  # 読み込み途中のまま

    observed = _analyze(
        _results_page(),
        _zero("age", age_settled, age_early),
        _zero("pagination", pagination_settled, thin_early),
    )

    assert observed.ready
    assert observed.ready[0].row_token == "div.c-search-member-card"
    assert observed.ready[0].empty_token in ("div.c-not-found", "div.c-not-found--searches")
    # ローダーは値にも別案にも出ない (消えたことが観測された一時要素)。
    assert all("c-loader" not in v.empty_token for v in observed.ready)
    _assert_no_frame_in_values(observed)


def test_a_late_mounting_overlay_cannot_masquerade_as_the_empty_state() -> None:
    """**実測5回目の形。** ツアー案内 (div.c-tour-guide) は結果ページの撮影時には
    まだマウントされておらず、0件ページの撮影時には在った。settled 2枚のXORを
    完璧に満たし、0件表示として **推奨されてしまった** -- 実際には数秒後の
    結果ページにも出る (クリック後の木に行と同居して写っていた)。

    行が1枚でも見えている観測ページ (クリック後の木・差し戻された0件ページ) を
    全部「結果側」に合流させることで塞ぐ。本物の0件表示 (c-not-found) が値になる。
    """
    tour = ("div", ("c-tour-guide",), 0)
    zero_display = ("div", ("c-not-found", "c-not-found--searches"), 3)
    zero_frame: list[tuple[str, tuple[str, ...], int]] = [
        ("body", ("c-body",), -1),
        ("div", ("c-segment",), 0),
        ("p", ("c-filter-text",), 1),
        ("div", ("c-segment",), 0),
    ]
    results = _results_page()  # 撮影時: ツアー未マウント
    age_settled = _tree(*zero_frame, zero_display, tour)  # 0件表示とツアーが同居
    # クリック後の結果ページ: 行とツアーが同居 = ツアーは0件表示ではない証拠
    after_click = _tree(
        *[(n.tag, n.class_names, n.parent) for n in results.nodes],
        ("div", ("c-tour-guide",), 0),
    )

    observed = analyze_candidate_list(
        ListCapture(
            requested_url=URL,
            landed_url=URL,
            results=results,
            zeros=(_zero("age", age_settled),),
            after_click=after_click,
        )
    )

    assert observed.ready
    assert observed.ready[0].empty_token in ("div.c-not-found", "div.c-not-found--searches")
    emitted = [v.empty_token for v in observed.ready] + [v.row_token for v in observed.ready]
    assert all("tour" not in token for token in emitted)


def test_a_bounced_zero_page_joins_the_results_side_evidence() -> None:
    """差し戻されて一覧が出たままの0件ページは「使えない」だけでなく、
    行と同居している要素の **結果側の証拠** としては使える。そこにだけ写った
    遅延マウント要素が、もう一方の0件ページで0件表示を名乗るのを防ぐ。"""
    tour = ("div", ("c-tour-guide",), 0)
    zero_display = ("div", ("c-not-found",), 3)
    zero_frame: list[tuple[str, tuple[str, ...], int]] = [
        ("body", ("c-body",), -1),
        ("div", ("c-segment",), 0),
        ("p", ("c-filter-text",), 1),
        ("div", ("c-segment",), 0),
    ]
    age_settled = _tree(*zero_frame, zero_display, tour)
    base = _results_page(cards=2, scouted_at=frozenset())
    bounced = _tree(
        *[(n.tag, n.class_names, n.parent) for n in base.nodes],
        ("div", ("c-tour-guide",), 0),
    )

    observed = _analyze(_results_page(), _zero("age", age_settled), _zero("pagination", bounced))

    assert observed.ready
    assert observed.ready[0].empty_token == "div.c-not-found"
    assert all("tour" not in v.empty_token for v in observed.ready)


def test_a_lingering_loader_on_the_zero_page_never_becomes_the_empty_state() -> None:
    """**反証レビューが再現した毒の経路。** age 変種の settled にローダーが残った
    まま撮影され (カードは消えているので使用可能)、骨組み片は剥がれたので除外は
    liberal 体制。次の pagination 遷移でローダーの消滅が観測されるが、直前 settled
    (=ローダー残留の age settled) に居るため、残像ガード付きの語彙では免除される。
    pagination 自体は差し戻しで使えない -- ページ間の突き合わせも働かない。

    候補除外に残像ガード **無し** の語彙 (vanished_tokens) を使うことで、
    ローダーが「最後の防壁」を抜けて値になる経路を塞ぐ。UNRESOLVED は許容する
    (見える失敗)。ローダー入りの値は許容しない (静かなゼロ件)。
    """
    loader = ("div", ("c-loader-view",), 0)
    splash = ("div", ("c-splash",), 0)  # 剥がれる骨組み片 (liberal 体制の鍵)
    zero_frame: list[tuple[str, tuple[str, ...], int]] = [
        ("body", ("c-body",), -1),
        ("div", ("c-segment",), 0),
        ("p", ("c-filter-text",), 1),
        ("div", ("c-segment",), 0),
    ]
    age_early = _tree(*zero_frame, loader, splash)
    age_settled = _tree(*zero_frame, loader)  # ローダーは残留、splash は剥がれた
    base = _results_page(cards=2, scouted_at=frozenset())
    pagination_early = _tree(*zero_frame, loader)  # age の続きに見える1枚
    pagination_settled = _tree(*[(n.tag, n.class_names, n.parent) for n in base.nodes])

    observed = _analyze(
        _results_page(),
        _zero("age", age_settled, age_early),
        _zero("pagination", pagination_settled, pagination_early),
    )

    emitted = [v.empty_token for v in observed.ready] + [v.row_token for v in observed.ready]
    assert all("loader" not in token for token in emitted)
