"""段階1の純粋部分を固定する。

ブラウザ操作そのものはテストできないので、テストできる形に切り出した部分
(SPA判定の材料・マーカー候補の抽出・観測結果の印字) をここで固定する (13.4)。
"""

from __future__ import annotations

from pathlib import Path

from jobmedley_scout.browser.dom import Clickable, one_line
from jobmedley_scout.recon.manual_login import (
    NO_DISPLAY_MESSAGE,
    LoginObservation,
    MarkerCandidate,
    headful_display_available,
    login_form_present_in_html,
    logout_texts_from,
    marker_candidates_from,
    marker_selector_candidates,
    selector_match_count,
)

SERVER_RENDERED = """
<html><body>
  <form action="/customers/sign_in" method="post">
    <input type="email" name="customer[email]">
    <input type="password" name="customer[password]">
    <button type="submit">ログイン</button>
  </form>
</body></html>
"""

SPA_SHELL = """
<html><body>
  <div id="root"></div>
  <script src="/assets/app-4f3a2b.js"></script>
</body></html>
"""


def test_password_field_in_served_html_means_not_a_spa() -> None:
    assert login_form_present_in_html(SERVER_RENDERED) is True


def test_empty_shell_means_probably_a_spa() -> None:
    """素のHTMLにパスワード欄が無い = JSが描画している = SPA の可能性が高い。"""
    assert login_form_present_in_html(SPA_SHELL) is False


def test_attribute_order_and_quoting_do_not_change_the_answer() -> None:
    """``type`` の書き方で判定が変わると、媒体次第で is_spa が逆になる。"""
    assert login_form_present_in_html('<input name="p" type=password>') is True
    assert login_form_present_in_html("<INPUT TYPE='PASSWORD'>") is True
    assert login_form_present_in_html('<input class="password" type="text">') is False


def test_candidates_are_ordered_most_stable_first() -> None:
    """id > クラス > テキスト。この順に画面変更へ強い。"""
    assert marker_selector_candidates("a", "logout-link", ("nav-item", "logout"), "ログアウト") == (
        "#logout-link",
        "a.nav-item",
        "a.logout",
        'a:has-text("ログアウト")',
    )


def test_hashed_class_names_are_rejected() -> None:
    """``css-1a2b3c4`` はビルドのたびに変わる。書いた瞬間から壊れる予定のセレクタ。

    落としても情報は失われない -- テキスト候補が残るので、運用者は必ず何か選べる。
    """
    candidates = marker_selector_candidates("button", None, ("css-1a2b3c4", "hdr"), "サインアウト")

    assert "button.css-1a2b3c4" not in candidates
    assert candidates == ("button.hdr", 'button:has-text("サインアウト")')


def test_hashed_id_is_rejected_too() -> None:
    """idも同じ。生成されたidを座標に書くと、次のデプロイで静かに空振りする。"""
    assert marker_selector_candidates("a", "ember1234abcdef", (), "ログアウト") == (
        'a:has-text("ログアウト")',
    )


def test_text_only_element_still_yields_a_candidate() -> None:
    """候補ゼロで返すと、運用者は何を書けばよいか分からないまま放り出される。"""
    assert marker_selector_candidates("a", None, (), " ログアウト ") == (
        'a:has-text("ログアウト")',
    )


def test_no_identifying_information_yields_nothing() -> None:
    """手掛かりが無いなら候補も出さない。**それらしい物を作らない** (原則3)。"""
    assert marker_selector_candidates("a", None, (), "   ") == ()


def test_report_names_the_coordinate_keys() -> None:
    """出力は座標キー名に紐づける。運用者はYAMLへ転記するだけで済む。"""
    report = LoginObservation(
        landed_url="https://customers.job-medley.com/customers/sign_in",
        login_form_in_served_html=True,
        marker_candidates=(MarkerCandidate("ログアウト", ("#logout",)),),
        session_path=Path("/tmp/creds/storage_state.json"),
    ).render()

    assert "auth.login_url: https://customers.job-medley.com/customers/sign_in" in report
    assert "auth.is_spa: false" in report
    assert "auth.success_marker_selector" in report
    assert "#logout" in report
    assert "auth.twofa_kind" in report


def test_report_warns_about_spa_submit_buttons() -> None:
    """SPAなら Enter が送信にならないことがある (5.5)。その注意が出ること。"""
    report = LoginObservation(
        landed_url="https://customers.job-medley.com/login",
        login_form_in_served_html=False,
        marker_candidates=(),
        session_path=Path("/tmp/creds/storage_state.json"),
    ).render()

    assert "auth.is_spa: true" in report
    assert "auth.submit_text_candidates" in report


def test_report_says_what_to_do_when_no_marker_was_found() -> None:
    """候補ゼロを黙って印字すると、運用者はそこで手が止まる。"""
    report = LoginObservation(
        landed_url="https://customers.job-medley.com/",
        login_form_in_served_html=True,
        marker_candidates=(),
        session_path=Path("/tmp/creds/storage_state.json"),
    ).render()

    assert "候補が見つかりませんでした" in report
    assert "開発者ツール" in report


def test_headless_linux_is_recognised_as_having_no_display() -> None:
    """クラウドの実行環境。ここで手動ログインを始めさせない。"""
    assert headful_display_available("linux", {}) is False


def test_linux_with_a_display_is_fine() -> None:
    assert headful_display_available("linux", {"DISPLAY": ":0"}) is True
    assert headful_display_available("linux", {"WAYLAND_DISPLAY": "wayland-0"}) is True


def test_macos_and_windows_are_never_blocked() -> None:
    """DISPLAY の概念が無いだけで、画面はある。ここで止めると手元でも動かない。"""
    assert headful_display_available("darwin", {}) is True
    assert headful_display_available("win32", {}) is True


def test_the_no_display_message_points_at_the_cloud_route() -> None:
    """止めるだけで代替を言わないと、運用者はそこで詰む。"""
    assert "JOBMEDLEY_SESSION_CURL" in NO_DISPLAY_MESSAGE
    assert "Copy as cURL" in NO_DISPLAY_MESSAGE


# --- 走査 (旧: ブラウザ相手のループ / 現: 平のデータに対する純粋関数) ------------


def _clickable(
    tag: str = "a", element_id: str | None = None, classes: tuple[str, ...] = (), text: str = ""
) -> Clickable:
    return Clickable(tag=tag, element_id=element_id, class_names=classes, text=text)


def test_logout_elements_become_marker_candidates() -> None:
    elements = [
        _clickable("a", "home", (), "トップ"),
        _clickable("a", "logout-link", ("nav",), "ログアウト"),
    ]

    assert marker_candidates_from(elements) == (
        MarkerCandidate("ログアウト", ("#logout-link", 'a:has-text("ログアウト")', "a.nav")),
    )


# --- 候補の並び順 (**ここを間違えるとマーカーが役目を失う**) --------------------


def test_a_generic_class_never_outranks_a_specific_one() -> None:
    """**実際に起きた形。**

    ログアウトリンクのクラスは ``c-link`` / ``c-link--alert`` /
    ``c-header-menu__logout-link`` の3つで、素朴に並べると汎用の ``a.c-link`` が
    先頭に来た。それは画面中のリンクほぼ全てに一致するので、**ログイン前の画面でも
    「マーカーあり」になる** -- 5.5 の判定が常に真を返し、認証切れを永久に検知
    できなくなる。
    """
    page = [
        _clickable("a", None, ("c-link",), "トップ"),
        _clickable("a", None, ("c-link",), "スカウト"),
        _clickable("a", None, ("c-link",), "メッセージ"),
        _clickable(
            "a", None, ("c-link", "c-link--alert", "c-header-menu__logout-link"), "ログアウト"
        ),
    ]

    candidates = marker_candidates_from(page)

    assert candidates[0].selectors[0] == "a.c-header-menu__logout-link"
    # 汎用クラスは捨てないが、**最後に回す**。選択肢は奪わない。
    assert candidates[0].selectors[-1] == "a.c-link"


def test_a_label_beats_a_class_that_does_not_name_its_purpose() -> None:
    """一致件数が同じなら、ログアウトという **文言** の方が安全側。

    文言はログイン前の画面には出ない。無関係なクラス名は出うる。守りたいのは
    「ログアウトしているのにマーカーが一致する」ことの防止なので、そちらを優先する。
    """
    page = [_clickable("a", None, ("u-mr8",), "ログアウト")]

    assert marker_candidates_from(page)[0].selectors == (
        'a:has-text("ログアウト")',
        "a.u-mr8",
    )


def test_match_counts_come_from_the_page_not_from_a_guess() -> None:
    page = [
        _clickable("a", None, ("c-link",), "トップ"),
        _clickable("a", None, ("c-link", "logout"), "ログアウト"),
    ]

    assert selector_match_count("a.c-link", page) == 2
    assert selector_match_count("a.logout", page) == 1
    assert selector_match_count('a:has-text("ログアウト")', page) == 1
    assert selector_match_count("a.absent", page) == 0


def test_the_two_scanners_agree_on_the_same_dom() -> None:
    """**これが今回の事故の核心。**

    同じ DOM に対して verify-session 側とマーカー抽出側の答えが食い違うと、
    一方が「入れている」と言い、他方が「ログイン後の要素が無い」と言う。実際に
    その報告が出た (原因は DOM 側の差だったが、走査が食い違わないこと自体を
    ここで固定しておく)。
    """
    elements = [
        _clickable("a", None, (), "ログアウト"),
        _clickable("button", None, (), "検索"),
    ]

    assert bool(logout_texts_from(elements)) is bool(marker_candidates_from(elements))
    assert logout_texts_from(elements) == ("ログアウト",)


def test_both_scanners_are_empty_on_an_empty_dom() -> None:
    """描画前の DOM。**「要素が無い」と「まだ描画されていない」は区別できない。**

    だから走査の前に待つ (:func:`browser.dom.wait_for_interactive`)。この関数の
    責務は待つことではないので、ここでは空を返すことだけを固定する。
    """
    assert marker_candidates_from([]) == ()
    assert logout_texts_from([]) == ()


def test_duplicate_labels_are_reported_once() -> None:
    elements = [
        _clickable("a", None, (), "ログアウト"),
        _clickable("button", None, (), "ログアウト"),
    ]

    assert logout_texts_from(elements) == ("ログアウト",)
    # 候補の方は重複させる -- セレクタが違うので、選択肢として意味がある。
    assert len(marker_candidates_from(elements)) == 2


def test_english_labels_are_recognised() -> None:
    assert logout_texts_from([_clickable("a", None, (), "Sign out")]) == ("Sign out",)


def test_unrelated_elements_are_ignored() -> None:
    elements = [_clickable("a", None, (), "スカウト"), _clickable("button", None, (), "検索")]

    assert marker_candidates_from(elements) == ()
    assert logout_texts_from(elements) == ()


def test_multiline_labels_never_reach_a_selector() -> None:
    """改行を含んだ文言が候補に混ざると、貼り付け用YAMLが壊れる。

    コメント行の ``#`` は1行目にしか付かないので、2行目以降が YAML の行として
    読まれる。``<button>ログ\\nアウト</button>`` のような改行入りの表示文字は
    珍しくないので、これは想定外ではなく通常のケースである。
    """
    assert one_line("ログ\n  アウト") == "ログ アウト"
    assert one_line("  \t 送信 \n") == "送信"

    candidates = marker_candidates_from([Clickable("a", None, (), one_line("ログ\nアウト"))])

    assert candidates == ()  # 「ログ アウト」は手掛かり語に一致しない


def test_a_normalised_label_still_yields_a_single_line_selector() -> None:
    candidates = marker_candidates_from([Clickable("a", None, (), one_line("  ログアウト \n "))])

    assert candidates[0].selectors == ('a:has-text("ログアウト")',)
    assert all("\n" not in selector for selector in candidates[0].selectors)


# --- 語彙の差し替え (閉じるボタン探索が同じ機構を使う) ----------------------------


def test_a_different_text_hint_vocabulary_finds_different_elements() -> None:
    """観測語彙 (LOGOUT vs CLOSE) を差し替えても、走査の仕組みは変わらない。"""
    elements = [_clickable("button", None, ("modal-close",), "閉じる")]

    assert marker_candidates_from(elements) == ()  # ログアウト語彙には一致しない
    assert marker_candidates_from(elements, text_hints=("閉じる",), purpose_tokens=("close",))

    close_candidates = marker_candidates_from(
        elements, text_hints=("閉じる",), purpose_tokens=("close",)
    )
    # クラス名が用途を名乗っている (modal-close) ので、文言一致より優先される。
    assert close_candidates[0].selectors[0] == "button.modal-close"


def test_icon_only_controls_are_found_via_aria_label() -> None:
    """×アイコンだけのボタンには文言が無い。aria-label でしか意味が分からない。"""
    element = Clickable("button", None, ("icon-btn",), "", aria_label="閉じる")

    candidates = marker_candidates_from(
        [element], text_hints=("閉じる",), purpose_tokens=("close",)
    )

    assert candidates[0].text == "閉じる"
    assert 'button[aria-label="閉じる"]' in candidates[0].selectors[0]


def test_aria_label_ranks_above_a_generic_class_but_below_id() -> None:
    assert marker_selector_candidates(
        "button", "close-btn", ("u-mr8",), "", aria_label="閉じる"
    ) == (
        "#close-btn",
        'button[aria-label="閉じる"]',
        "button.u-mr8",
    )
