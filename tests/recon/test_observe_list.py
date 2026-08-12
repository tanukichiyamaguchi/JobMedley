"""段階2の座標を「観測して印字する」部分を固定する。

守りたいのは observe_login と同じ2点 (:mod:`tests.recon.test_observe_login` 参照)。

1. **観測できたものだけを値として出す。** それらしい値を作らない (原則3)
2. **観測できなかったものは UNRESOLVED のまま出す。**

加えて段階2固有の要点: ``nav.list_ready_selector`` は **行ではなくコンテナ** で
なければならない。行を選ぶと、0件の検索結果を「まだ描画されていない」と誤読する。
"""

from __future__ import annotations

import textwrap

import yaml

from jobmedley_scout.browser.dom import ClassedElement, SelectField
from jobmedley_scout.config.coordinates import COORDINATES_BY_KEY
from jobmedley_scout.config.placeholders import LadderStage
from jobmedley_scout.recon.manual_login import MarkerCandidate
from jobmedley_scout.recon.observe_list import (
    STAGE_2_KEYS,
    ObservedList,
    class_frequency,
    list_ready_candidates,
    rows_that_vanish_on_empty_results,
    select_selector_candidates,
    selection_redirected,
    zero_result_variant,
)

REQUESTED = "https://customers.job-medley.com/customers/searches?age[from]=0&age[to]=40"


def _observed(**overrides: object) -> ObservedList:
    defaults: dict[str, object] = {"requested_url": REQUESTED}
    defaults.update(overrides)
    return ObservedList(**defaults)  # type: ignore[arg-type]


# --- 座標キーの網羅性 --------------------------------------------------------


def test_stage_2_keys_are_exactly_the_ones_this_module_fills() -> None:
    """段階1/2の観測で確定済みの2つ (nav.mypage_url, nav.candidate_list_url) は含まない。"""
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


# --- zero_result_variant -------------------------------------------------------


def test_age_bounds_are_pushed_past_any_real_age() -> None:
    variant = zero_result_variant("https://a.example/s?age[from]=0&age[to]=40&gdr[0]=1")

    assert variant is not None
    assert "age[from]=120" in variant
    assert "age[to]=121" in variant
    assert "gdr[0]=1" in variant  # 他のパラメータは保つ


def test_no_age_parameter_means_no_variant_can_be_built() -> None:
    """**それらしい変種を作らない。** 比較できないなら None とだけ言う (原則3)。"""
    assert zero_result_variant("https://a.example/s?foo=1") is None


# --- class_frequency / list_ready_candidates / rows_that_vanish ---------------


def test_hashy_class_names_are_not_counted() -> None:
    elements = [ClassedElement("div", ("css-1a2b3c4",)), ClassedElement("div", ("card",))]

    assert class_frequency(elements) == {"div.card": 1}


def test_a_row_class_is_recommended_against_but_a_container_class_is_recommended() -> None:
    """**実際に守りたい区別。** 行は0件で消える。コンテナは残る。

    ヘッダーのような「結果件数に関わらず存在する」他の要素も、0件比較を
    生き延びる限りは候補になる -- ただし件数が少ないもの (より限定的なもの) が
    先頭に来る。
    """
    with_results = {"li.candidate-card": 25, "section.search-results": 1, "header.c-hdr": 3}
    zero_result = {"section.search-results": 1, "header.c-hdr": 3}

    assert list_ready_candidates(with_results, zero_result) == (
        "section.search-results",
        "header.c-hdr",
    )
    assert rows_that_vanish_on_empty_results(with_results, zero_result) == ("li.candidate-card",)


def test_a_single_occurrence_is_never_reported_as_a_row() -> None:
    """1回しか無い要素は行ではなく見出し等かもしれない。行として報告しない。"""
    with_results = {"h1.page-title": 1}
    zero_result: dict[str, int] = {}

    assert rows_that_vanish_on_empty_results(with_results, zero_result) == ()


def test_container_candidates_prefer_stable_counts() -> None:
    """件数の変動が小さいものほど、真のコンテナらしい。"""
    with_results = {"div.wrapper": 1, "div.list-shell": 2}
    zero_result = {"div.wrapper": 1, "div.list-shell": 1}

    assert list_ready_candidates(with_results, zero_result) == ("div.wrapper", "div.list-shell")


# --- select_selector_candidates ------------------------------------------------


def test_a_single_option_select_is_not_a_real_choice() -> None:
    fields = [SelectField(element_id=None, name="branch", option_count=1)]

    assert select_selector_candidates(fields) == ()


def test_a_multi_option_select_becomes_a_candidate() -> None:
    fields = [SelectField(element_id="group-select", name=None, option_count=3)]

    assert select_selector_candidates(fields) == ("#group-select",)


# --- render(): セッションが無い/効いていない ------------------------------------


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


# --- render(): 選択ステップ -----------------------------------------------------


def test_a_redirect_is_reported_as_selection_required() -> None:
    report = _observed(
        landed_url="https://customers.job-medley.com/customers/groups/select",
        selection_required=True,
        select_candidates=("#group-select",),
    ).render()

    assert "context.selection_required: true" in report
    assert 'context.selector: "#group-select"' in report


def test_selection_required_with_no_select_found_stays_unresolved() -> None:
    report = _observed(landed_url="https://x/select", selection_required=True).render()

    assert "context.selector: UNRESOLVED" in report
    assert "開発者ツールで探してください" in report


def test_selection_required_leaves_the_downstream_coordinates_unresolved() -> None:
    """選択ステップの先まで行けなかったので、残り2つは観測していない。嘘をつかない。"""
    report = _observed(
        landed_url="https://x/select", selection_required=True, select_candidates=("#g",)
    ).render()

    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "context.selector を" in report


def test_no_redirect_means_selection_is_not_required() -> None:
    report = _observed(landed_url=REQUESTED, selection_required=False).render()

    assert "context.selection_required: false" in report
    assert "context.selector: null" in report


# --- render(): nav.list_ready_selector ------------------------------------------


def test_no_zero_result_comparison_leaves_list_ready_unresolved() -> None:
    report = _observed(landed_url=REQUESTED, zero_result_comparable=False).render()

    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "age[from]/age[to]" in report
    assert "行のコンテナを選ぶこと" in report


def test_a_found_container_becomes_the_value_and_rows_become_a_warning() -> None:
    report = _observed(
        landed_url=REQUESTED,
        zero_result_comparable=True,
        list_ready_candidates=("section.search-results", "div.wrapper"),
        list_ready_vanishing_rows=("li.candidate-card",),
    ).render()

    assert 'nav.list_ready_selector: "section.search-results"' in report
    assert "別案: div.wrapper" in report
    assert "避けるべき候補" in report
    assert "li.candidate-card" in report


def test_no_surviving_container_stays_unresolved_even_with_vanishing_rows() -> None:
    """コンテナが無いなら、行を仕方なく採用したりしない。"""
    report = _observed(
        landed_url=REQUESTED,
        zero_result_comparable=True,
        list_ready_vanishing_rows=("li.candidate-card",),
    ).render()

    assert "nav.list_ready_selector: UNRESOLVED" in report
    assert "li.candidate-card" in report  # 参考として出すが、値には使わない


# --- render(): nav.drawer_close_selectors --------------------------------------


def test_no_row_found_means_the_drawer_was_never_tried() -> None:
    report = _observed(landed_url=REQUESTED, zero_result_comparable=True).render()

    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "候補者の行を特定できなかった" in report


def test_a_click_that_opens_nothing_is_reported_honestly() -> None:
    report = _observed(
        landed_url=REQUESTED,
        drawer_row_selector_tried="li.candidate-card",
        drawer_attempted=True,
        drawer_opened=False,
    ).render()

    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "新しい要素の出現を検知できませんでした" in report
    assert "li.candidate-card" in report


def test_an_opened_drawer_with_no_close_button_found_says_so() -> None:
    report = _observed(
        landed_url=REQUESTED,
        drawer_row_selector_tried="li.candidate-card",
        drawer_attempted=True,
        drawer_opened=True,
        drawer_evidence=("div.drawer", "p.summary"),
    ).render()

    assert "nav.drawer_close_selectors: UNRESOLVED" in report
    assert "閉じるボタンらしき要素が見つかりませんでした" in report
    assert "div.drawer" in report


def test_close_candidates_become_a_fallback_list_in_priority_order() -> None:
    report = _observed(
        landed_url=REQUESTED,
        drawer_row_selector_tried="li.candidate-card",
        drawer_attempted=True,
        drawer_opened=True,
        close_candidates=(
            MarkerCandidate("閉じる", ("button.drawer-close", 'button:has-text("閉じる")')),
        ),
    ).render()

    assert 'nav.drawer_close_selectors: ["button.drawer-close"]' in report
    assert "総当たりして駄目なら Escape" in report
    assert "button:has-text" in report


# --- yaml_block(): 実際に読み込ませる ---------------------------------------------


def test_the_full_success_case_parses_as_yaml() -> None:
    parsed = yaml.safe_load(
        textwrap.dedent(
            _observed(
                landed_url=REQUESTED,
                zero_result_comparable=True,
                list_ready_candidates=("section.search-results",),
                list_ready_vanishing_rows=("li.candidate-card",),
                drawer_row_selector_tried="li.candidate-card",
                drawer_attempted=True,
                drawer_opened=True,
                close_candidates=(MarkerCandidate("閉じる", ("button.drawer-close",)),),
            ).yaml_block()
        )
    )

    assert parsed == {
        "context.selection_required": False,
        "context.selector": None,
        "nav.list_ready_selector": "section.search-results",
        "nav.drawer_close_selectors": ["button.drawer-close"],
    }


def test_every_rendered_scenario_covers_exactly_the_stage_2_keys() -> None:
    """1つでも欠けたり、余計なキーが混ざったりすると、運用者は転記でつまずく。"""
    scenarios = [
        _observed(session_present=False),
        _observed(session_expired=True, landed_url="https://x/sign_in"),
        _observed(landed_url="https://x/select", selection_required=True),
        _observed(landed_url=REQUESTED, zero_result_comparable=False),
        _observed(landed_url=REQUESTED, zero_result_comparable=True),
    ]
    for scenario in scenarios:
        parsed = yaml.safe_load(textwrap.dedent(scenario.yaml_block()))
        assert set(parsed) == set(STAGE_2_KEYS), scenario
