"""段階1の純粋部分を固定する。

ブラウザ操作そのものはテストできないので、テストできる形に切り出した部分
(SPA判定の材料・マーカー候補の抽出・観測結果の印字) をここで固定する (13.4)。
"""

from __future__ import annotations

from pathlib import Path

from jobmedley_scout.recon.manual_login import (
    NO_DISPLAY_MESSAGE,
    LoginObservation,
    MarkerCandidate,
    headful_display_available,
    login_form_present_in_html,
    marker_selector_candidates,
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
