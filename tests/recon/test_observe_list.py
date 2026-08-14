"""段階2の座標を「観測して印字する」部分を固定する。

守りたいのは observe_login と同じ2点 (:mod:`tests.recon.test_observe_login` 参照)。

1. **観測できたものだけを値として出す。** それらしい値を作らない (原則3)
2. **観測できなかったものは UNRESOLVED のまま出す** -- ただし **理由を添えて**

段階2固有の要点は、2026-08-13 に実際に起きた誤りである。``nav.list_ready_selector``
に ``body.c-body`` (全ページに常時ある枠) を推奨してしまった。値の選び方そのものは
:mod:`tests.recon.test_list_structure` が検査している。ここでは **報告** の側 --
値が出せないときに何が起きたかを運用者が追えるか -- を固定する。
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from jobmedley_scout.browser.dom import Clickable, SelectField
from jobmedley_scout.config.coordinates import COORDINATES_BY_KEY
from jobmedley_scout.config.placeholders import LadderStage
from jobmedley_scout.recon.list_structure import EmptyCandidate, ReadyValue, RowGroup
from jobmedley_scout.recon.manual_login import MarkerCandidate
from jobmedley_scout.recon.observe_list import (
    STAGE_2_KEYS,
    ObservedList,
    ZeroPage,
    newly_visible_clickables,
    select_selector_candidates,
    selection_redirected,
    zero_page_is_usable,
    zero_result_variants,
)

REQUESTED = (
    "https://customers.job-medley.com/customers/searches"
    "?age[from]=0&age[to]=40&pagination[page]=1&gdr[0]=1"
)

ROW = RowGroup(token="div.c-search-member-card", parent=4, members=(5, 10), subtree_total=10)
EMPTY = EmptyCandidate(
    token="div.c-search-empty", depth_from_anchor=1, counts_zero=(1,), scope="region"
)
READY = ReadyValue("div.c-search-member-card", "div.c-search-empty")


def _observed(**overrides: object) -> ObservedList:
    defaults: dict[str, object] = {"requested_url": REQUESTED, "landed_url": REQUESTED}
    defaults.update(overrides)
    return ObservedList(**defaults)  # type: ignore[arg-type]


def _found(**overrides: object) -> ObservedList:
    """A fully successful observation."""
    defaults: dict[str, object] = {
        "tree_read": True,
        "row_groups": (ROW,),
        "rows_confirmed_vanishing": True,
        "empty_candidates": (EMPTY,),
        "zero_pages": (("age", True, ""), ("pagination", True, "")),
        "ready": (READY,),
        "empty_state_scope": "region",
        "anchor_token": "div.js-infinity-scroll-outer-el",
    }
    defaults.update(overrides)
    return _observed(**defaults)


# --- 座標キーの網羅性 --------------------------------------------------------


def test_stage_2_keys_are_exactly_the_ones_this_module_fills() -> None:
    stage_two = {
        key
        for key, spec in COORDINATES_BY_KEY.items()
        if spec.stage is LadderStage.STAGE_2_PREFLIGHT
    }
    already_confirmed = {"nav.mypage_url", "nav.candidate_list_url"}

    assert set(STAGE_2_KEYS) == stage_two - already_confirmed


# --- selection_redirected -----------------------------------------------------


def test_same_path_different_query_is_not_a_redirect() -> None:
    """検索条件のクエリはリダイレクトで正規化されうる。クエリの一致までは求めない。"""
    assert (
        selection_redirected(
            "https://a.example/customers/searches?age[from]=0",
            "https://a.example/customers/searches?age%5Bfrom%5D=0&x=1",
        )
        is False
    )


def test_a_different_path_is_a_redirect() -> None:
    assert (
        selection_redirected(
            "https://a.example/customers/searches", "https://a.example/customers/groups/select"
        )
        is True
    )


def test_a_trailing_slash_does_not_count_as_a_redirect() -> None:
    assert selection_redirected("https://a.example/x", "https://a.example/x/") is False


# --- zero_result_variants -----------------------------------------------------


def test_two_independent_mechanisms_produce_two_variants() -> None:
    """**独立な2機構で0件を作る。** 片方でしか出ない要素は別物かもしれない。"""
    variants = zero_result_variants(REQUESTED)

    assert [v.kind for v in variants] == ["age", "pagination"]
    assert "age[from]=120" in variants[0].url and "age[to]=121" in variants[0].url
    assert "pagination[page]=9999" in variants[1].url
    # 他のパラメータは保つ。
    assert all("gdr[0]=1" in v.url for v in variants)


def test_a_url_without_either_parameter_yields_no_variant() -> None:
    """**それらしい変種を作らない。** 作れないなら空を返すだけ (原則3)。"""
    assert zero_result_variants("https://a.example/s?foo=1") == ()


def test_one_mechanism_still_gives_one_variant() -> None:
    assert [v.kind for v in zero_result_variants("https://a.example/s?age[to]=40")] == ["age"]


# --- zero_page_is_usable ------------------------------------------------------


def _zero(**overrides: object) -> ZeroPage:
    defaults: dict[str, object] = {
        "kind": "age",
        "landed_url": REQUESTED,
        "tree_read": True,
        "tree_truncated": False,
        "counts": {},
        "vanished_repeated_count": 3,
        "remaining_repeated_count": 1,
        "early_counts": {},
    }
    defaults.update(overrides)
    return ZeroPage(**defaults)  # type: ignore[arg-type]


def test_a_clean_zero_page_is_usable_and_carries_diagnostics() -> None:
    """使える場合も内訳を返す。報告に印字され、スナップショットと突き合わせられる。"""
    usable, why = zero_page_is_usable(_zero(), REQUESTED)
    assert usable is True
    assert "消えた繰り返し 3種" in why


def test_an_unchanged_zero_page_is_refused() -> None:
    """**判定の土台を無検査で信頼しない。**

    ``goto`` は遷移失敗を握り潰す (5.3 のため意図的にそうしてある)。遷移が
    失敗して結果ページのままだと、全トークンが「両ページに存在」になり、
    いま直したはずの ``body.c-body`` がまた通る。「繰り返しが1つも消えていない」は
    その状態の観測である。
    """
    usable, why = zero_page_is_usable(
        _zero(vanished_repeated_count=0, remaining_repeated_count=4), REQUESTED
    )

    assert usable is False
    assert "1つも消えていません" in why


def test_the_zero_page_check_does_not_depend_on_nesting_or_on_the_row() -> None:
    """**この判定は3代目である** (:class:`ZeroPage` の docstring に経緯)。

    1代目は行の同定に依存して巻き添えを起こし (実測)、2代目 (最も重い繰り返し群) は
    部分木の重さが入れ子の外側を優遇するため、一覧を **囲む** 区画
    (``div.c-segment`` ×2) が中身ごと数えられてカード25枚より重くなり、
    正常な0件ページを2枚とも拒否した (variant電池が実機の前に検出した)。

    現在の判定は「種類が消えたか」だけを見る。入れ子の形にも行の同定にも依存しない。
    """
    usable, note = zero_page_is_usable(
        _zero(vanished_repeated_count=1, remaining_repeated_count=9), REQUESTED
    )

    assert usable is True
    # 残存の内訳は診断として返り、報告に印字される (スナップショットと突き合わせ用)。
    assert "残存 9種" in note


def test_a_redirected_zero_page_is_refused() -> None:
    """セッション切れもここで捕まる (失効するとサインイン画面へ転送される)。"""
    usable, why = zero_page_is_usable(
        _zero(landed_url="https://customers.job-medley.com/customers/sign_in/"), REQUESTED
    )

    assert usable is False
    assert "転送" in why


def test_an_unreadable_zero_page_is_refused_and_says_it_was_not_empty() -> None:
    """**「読めなかった」を「空だった」と読まない。** それが原則2の再生産になる。"""
    usable, why = zero_page_is_usable(_zero(tree_read=False), REQUESTED)

    assert usable is False
    assert "要素が無かったのではありません" in why


def test_a_truncated_zero_page_is_refused() -> None:
    usable, _why = zero_page_is_usable(_zero(tree_truncated=True), REQUESTED)
    assert usable is False


# --- newly_visible_clickables -------------------------------------------------


def test_identical_looking_buttons_are_still_counted_as_new() -> None:
    """素朴な集合差だと、既存と tag/class/文言が同一な要素の増加を見落とす。

    ドロワーの中に一覧と同じ形のボタンが並ぶ作りは珍しくない。
    """
    same = Clickable("button", None, ("c-button",), "送信")
    fresh = newly_visible_clickables([same], [same, same])

    assert fresh == (same,)


def test_nothing_new_yields_nothing() -> None:
    same = Clickable("a", None, ("c-link",), "トップ")
    assert newly_visible_clickables([same], [same]) == ()


# --- select_selector_candidates ------------------------------------------------


def test_a_single_option_select_is_not_a_real_choice() -> None:
    assert select_selector_candidates([SelectField(None, "branch", 1)]) == ()


def test_a_multi_option_select_becomes_a_candidate() -> None:
    assert select_selector_candidates([SelectField("group-select", None, 3)]) == ("#group-select",)


# --- render(): 早期の失敗 -------------------------------------------------------


def test_no_saved_session_leaves_every_key_unresolved() -> None:
    report = _observed(session_present=False).render()

    for key in STAGE_2_KEYS:
        assert f"{key}: UNRESOLVED" in report
    assert "JOBMEDLEY_SESSION_CURL" in report


def test_an_expired_session_is_never_reported_as_a_missing_coordinate() -> None:
    """段階1で踏んだ取り違えと同じ形。パスワード欄が見えたなら一覧を見ていない。"""
    report = _observed(
        session_expired=True,
        landed_url="https://customers.job-medley.com/customers/sign_in/",
    ).render()

    for key in STAGE_2_KEYS:
        assert f"{key}: UNRESOLVED" in report
    assert "セッションが効いていません" in report
    assert "https://customers.job-medley.com/customers/sign_in/" in report


def test_an_unreadable_tree_is_never_reported_as_an_empty_page() -> None:
    """**「読めなかった」と「無かった」を区別する。**"""
    report = _observed(tree_read=False).render()

    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "要素が無かったのではありません" in report


def test_a_truncated_tree_refuses_to_emit_a_value() -> None:
    """末尾の行が欠けているかもしれない状態で共通祖先を計算しない。"""
    report = _observed(tree_read=True, tree_truncated=True).render()

    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "打ち切られました" in report


# --- render(): 選択ステップ -----------------------------------------------------


def test_a_redirect_is_reported_as_selection_required() -> None:
    report = _observed(
        landed_url="https://customers.job-medley.com/customers/groups/select",
        selection_required=True,
        select_candidates=("#group-select",),
    ).render()

    assert "context.selection_required: true" in report
    assert 'context.selector: "#group-select"' in report


def test_selection_required_leaves_the_downstream_coordinates_unresolved() -> None:
    """選択ステップの先まで行けなかったので、残り2つは観測していない。嘘をつかない。"""
    report = _observed(
        landed_url="https://x/select", selection_required=True, select_candidates=("#g",)
    ).render()

    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "nav.drawer_close_selectors: UNRESOLVED" in report


def test_no_redirect_means_selection_is_not_required() -> None:
    report = _found().render()

    assert "context.selection_required: false" in report
    assert "context.selector: null" in report


# --- render(): nav.list_ready_selector -------------------------------------------


def test_the_value_is_a_selector_list_covering_both_signals() -> None:
    report = _found().render()

    assert 'nav.list_ready_selector: "div.c-search-member-card, div.c-search-empty"' in report
    assert "論理和" in report


def test_the_report_never_offers_a_frame_selector() -> None:
    """**実際に推奨してしまった値が、どの欄にも現れないこと。**"""
    report = _found().render()

    assert "body.c-body" not in report


def test_no_zero_page_at_all_says_so_and_refuses_to_guess() -> None:
    report = _observed(tree_read=True, row_groups=(ROW,)).render()

    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "0件になる検索条件を1つも作れませんでした" in report


def test_each_zero_page_attempt_is_reported_with_its_reason() -> None:
    """どの機構がなぜ使えなかったのかが分からないと、運用者は切り分けられない。"""
    report = _observed(
        tree_read=True,
        row_groups=(ROW,),
        zero_pages=(("age", False, "別画面へ転送されました (到達URL: https://x/sign_in)"),),
    ).render()

    assert "age: 使えません — 別画面へ転送されました" in report


def test_rows_without_an_empty_state_give_a_template_not_a_bare_row() -> None:
    """**行トークン単独を値にしない。** 0件の検索が永久に待たされる。"""
    report = _observed(
        tree_read=True,
        row_groups=(ROW,),
        rows_confirmed_vanishing=True,
        zero_pages=(("age", True, ""),),
    ).render()

    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "行トークン単独を値にしないこと" in report
    assert "div.c-search-member-card, <0件表示のセレクタ>" in report


def test_no_rows_reports_the_shadow_dom_it_could_not_see() -> None:
    """走査できなかった領域を数として残す。「空だった」で終わらせない。"""
    report = _observed(tree_read=True, shadow_root_count=3).render()

    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "行らしい繰り返し構造を見つけられませんでした" in report
    assert "影DOM" in report


# --- render(): 添える注記 (**確かめられなかったことを黙らない**) ------------------


def test_a_single_zero_page_says_the_risk_out_loud() -> None:
    report = _found(empty_state_single_variant=True).render()

    assert "1種類しか使えませんでした" in report
    assert "見える失敗" in report


def test_an_unconfirmed_row_says_so() -> None:
    report = _found(rows_confirmed_vanishing=False).render()

    assert "0件検索で消えることは確認していません" in report


def test_a_widened_search_scope_is_disclosed() -> None:
    """黙って画面全体へ広げない。"""
    report = _found(empty_state_scope="page").render()

    assert "画面全体から探しました" in report


def test_rows_outside_the_list_are_counted_in_the_report() -> None:
    report = _found(rows_outside_group=2).render()

    assert "一覧の外にも 2 個" in report


def test_the_class_name_lifetime_caveat_is_always_present() -> None:
    """篩は16進の並びしか弾けない。1回の観測で寿命は分からない。"""
    assert "1回の観測では分かりません" in _found().render()


# --- render(): nav.drawer_close_selectors ----------------------------------------


def test_no_safe_click_target_refuses_to_click_and_says_why() -> None:
    """**取り消せない外向き操作を偵察で踏まない。**"""
    report = _found().render()  # drawer_attempted=False, row_groups あり

    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "js-tour-guide-scout-button" in report
    assert "押せる場所が確実でなければ押しません" in report


def test_a_click_that_navigates_is_reported_as_navigation_not_failure() -> None:
    """ページ遷移とドロワーは別物。取り違えると座標の要否を誤る。"""
    report = _found(
        drawer_attempted=True,
        drawer_click_locator=("div.c-search-member-card__field", 0),
        drawer_url_changed=True,
        landed_url="https://customers.job-medley.com/customers/members/1",
    ).render()

    assert "ページ遷移" in report
    assert "この座標は不要かもしれません" in report


def test_a_click_that_never_completed_is_not_reported_as_pressed() -> None:
    """**実測4回目の誤導を塞ぐ。** クリックが完了しなかった (操作可能性の検査で
    満了した) のに「押しましたが新出要素なし」と報告し、ドロワーの謎を
    「押しても開かない」問題だと誤認させた。押せていないなら押せていないと言う。
    """
    report = _found(
        drawer_attempted=True,
        drawer_click_locator=("div.c-search-member-card__main-content", 0),
        drawer_click_failed=True,
        drawer_covering=("div.c-tour-guide__tooltip", "div.c-overlay", "body.c-body"),
    ).render()

    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "完了しませんでした" in report
    assert "押しましたが" not in report
    assert "div.c-tour-guide__tooltip" in report
    # 覆い要素にツアー系の語があれば、実画面での解消手順まで案内する。
    assert "実画面で一度案内を閉じてから再実行" in report


def test_a_failed_dismissal_of_the_tour_is_reported() -> None:
    """閉じを試みて失敗した事実は、遅延マウントの形でも必ず印字される。

    反証レビューで、2回目の閉じ試行の失敗 (False) が捨てられ、この診断行が
    「それが書かれた当のシナリオで一度も印字されない」ことが確認された。
    """
    report = _found(
        drawer_attempted=True,
        drawer_click_locator=("div.c-search-member-card__main-content", 0),
        drawer_click_failed=True,
        drawer_covering=("a.c-tour-guide__overlay", "div.c-tour-guide"),
        tour_dismiss_failed=True,
    ).render()

    assert "閉じられませんでした" in report


def test_a_failed_click_without_covering_evidence_stays_honest() -> None:
    """遮り要素を読めなかったときに「遮り無し」と断定しない。"""
    report = _found(
        drawer_attempted=True,
        drawer_click_locator=("div.c-search-member-card__main-content", 0),
        drawer_click_failed=True,
    ).render()

    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "完了しませんでした" in report
    assert "覆っていた要素" not in report


def test_a_click_that_opens_nothing_is_reported_honestly() -> None:
    report = _found(
        drawer_attempted=True,
        drawer_click_locator=("div.c-search-member-card__field", 0),
        drawer_opened=False,
    ).render()

    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "新しい要素の出現を検知できませんでした" in report
    assert "div.c-search-member-card__field の 0 番目" in report


def test_an_opened_drawer_with_no_close_button_shows_the_structure() -> None:
    report = _found(
        drawer_attempted=True,
        drawer_click_locator=("div.x", 0),
        drawer_opened=True,
        drawer_evidence=("div.c-side-cover", "p.summary"),
    ).render()

    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "閉じるボタンらしき要素が見つかりませんでした" in report
    assert "div.c-side-cover" in report
    assert "文言は出しません" in report


def test_close_candidates_become_a_fallback_list() -> None:
    report = _found(
        drawer_attempted=True,
        drawer_click_locator=("div.x", 0),
        drawer_opened=True,
        close_candidates=(
            MarkerCandidate("閉じる", ("a.c-side-cover__close-btn", 'a:has-text("閉じる")')),
        ),
    ).render()

    assert 'nav.drawer_close_selectors: ["a.c-side-cover__close-btn"]' in report
    assert "総当たりして駄目なら Escape" in report


# --- yaml_block(): 実際に読み込ませる ---------------------------------------------


def test_the_full_success_case_parses_as_yaml() -> None:
    parsed = yaml.safe_load(
        textwrap.dedent(
            _found(
                drawer_attempted=True,
                drawer_click_locator=("div.x", 0),
                drawer_opened=True,
                close_candidates=(MarkerCandidate("閉じる", ("a.c-side-cover__close-btn",)),),
            ).yaml_block()
        )
    )

    assert parsed == {
        "context.selection_required": False,
        "context.selector": None,
        "nav.list_ready_selector": "div.c-search-member-card, div.c-search-empty",
        "nav.drawer_close_selectors": ["a.c-side-cover__close-btn"],
    }


@pytest.mark.parametrize(
    "scenario",
    [
        _observed(session_present=False),
        _observed(session_expired=True, landed_url="https://x/sign_in"),
        _observed(landed_url="https://x/select", selection_required=True),
        _observed(tree_read=False),
        _observed(tree_read=True, tree_truncated=True),
        _observed(tree_read=True),
        _observed(tree_read=True, row_groups=(ROW,)),
        _found(),
    ],
)
def test_every_scenario_emits_exactly_the_stage_2_keys(scenario: ObservedList) -> None:
    """1つでも欠けたり、余計なキーが混ざったりすると、運用者は転記でつまずく。"""
    parsed = yaml.safe_load(textwrap.dedent(scenario.yaml_block()))

    assert set(parsed) == set(STAGE_2_KEYS)


# --- 反証で見つかった報告側の穴 --------------------------------------------------


def test_an_unverified_row_never_gets_a_paste_ready_template() -> None:
    """**実測でここが牙を剥いた。**

    0件ページが2枚とも使えなかった実行で、行が ``div.c-segment`` (画面の区画) と
    誤判定されたまま ``"div.c-segment, <0件表示のセレクタ>"`` という記入例を印字して
    いた。``c-segment`` は描画前から常に在るので、指示どおり貼れば **常に真になる
    目印が座標に入る** -- このモジュールが潰すはずの失敗を、こちらから勧めていた。
    """
    report = _observed(
        tree_read=True,
        row_groups=(RowGroup(token="div.c-segment", parent=0, members=(1, 3), subtree_total=8),),
        rows_confirmed_vanishing=False,
        zero_pages=(("age", False, "一覧の繰り返し要素が 1 個残っています"),),
    ).render()

    assert "nav.list_ready_selector: UNRESOLVED" in report
    # 記入例を出さない。
    assert "div.c-segment, <0件表示のセレクタ>" not in report
    # 確認していないことを明示する。
    assert "0件検索で消えることは" in report
    assert "そのまま座標に書かないでください" in report
    # 参考としては出す (運用者が手で確認する手掛かりになるため)。
    assert "参考: div.c-segment" in report


def test_a_verified_row_still_gets_the_template() -> None:
    """確認できた行なら、記入例を出してよい。守りが行き過ぎていないこと。"""
    report = _observed(
        tree_read=True,
        row_groups=(ROW,),
        rows_confirmed_vanishing=True,
        zero_pages=(("age", True, ""),),
    ).render()

    assert "0件検索で消えることを確認済み" in report
    assert "div.c-search-member-card, <0件表示のセレクタ>" in report
