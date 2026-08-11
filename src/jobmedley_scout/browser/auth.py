"""Authentication: saved session first, password login as fallback.

5.4: 認証は二経路で実装する。

**経路1 (優先): 保存セッションの持ち込み。** ローカルでヘッドフル起動して人間が
ログインし、セッションをファイルに保存 → base64 化して CI のシークレットへ →
実行時に復元。この経路が必要なのは、**データセンターのIPアドレスからの自動ログインが
媒体の2段階認証やボット検知で失敗しやすい** ため。CI側で2段階認証を突破する手段が
ないので、人が一度突破した結果を持ち込む以外に方法がない。

**経路2 (フォールバック): メールアドレスとパスワードによる自動ログイン。**
認証情報が未設定なら即座にエラーで停止する。

5.5: **ログイン成功はマーカー要素の存在で判定する** -- 遷移の完了やステータス
コードではなく、ログイン後にのみ存在する要素 (「ログアウト」リンクなど)。
セレクタは座標なので、画面変更時にコードを触らずに追従できる。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.navigation import goto, marker_present, wait_for_marker
from jobmedley_scout.browser.waits import pause
from jobmedley_scout.config.placeholders import Coord, is_resolved, require
from jobmedley_scout.config.schema import BrowserConfig, WaitsConfig
from jobmedley_scout.config.secrets import Secrets
from jobmedley_scout.errors import PermanentAuthError


class AuthRoute(StrEnum):
    SAVED_SESSION = "saved_session"
    PASSWORD_LOGIN = "password_login"
    MANUAL = "manual"


@dataclass(frozen=True)
class AuthResult:
    route: AuthRoute
    authenticated: bool
    detail: str


def is_authenticated(page: Any, marker_selector: str, config: BrowserConfig) -> bool:
    """5.5: マーカー要素の存在だけで判定する。"""
    return marker_present(page, marker_selector, timeout_ms=config.selector_timeout_ms)


def ensure_authenticated(
    page: Any,
    context: Any,
    *,
    login_url: Coord[str],
    marker_selector: Coord[str],
    email_selector: Coord[str],
    password_selector: Coord[str],
    submit_selector: Coord[str],
    submit_text_candidates: Coord[tuple[str, ...]],
    is_spa: Coord[bool],
    secrets: Secrets,
    credentials_dir: Path,
    config: BrowserConfig,
    waits: WaitsConfig,
) -> AuthResult:
    """Get to an authenticated state, by whichever route is available."""
    marker = require(marker_selector, used_by="browser.auth.ensure_authenticated")
    url = require(login_url, used_by="browser.auth.ensure_authenticated")

    # 経路1: 既に保存セッションで入れているか。
    goto(page, url, config)
    if is_authenticated(page, marker, config):
        # ログイン成功直後にも保存する (5.4)。セッションは更新されうるため。
        session_store.save(context, credentials_dir)
        return AuthResult(AuthRoute.SAVED_SESSION, True, "保存セッションで認証済み")

    # 経路2: メール/パスワード。認証情報が無ければ即座に停止する。
    if not secrets.has_password_login():
        raise PermanentAuthError(
            "保存セッションが無効で、メール/パスワードも未設定です。認証経路がありません。\n"
            "ローカルで `scout recon login` をヘッドフル実行して手動ログインし、"
            "`scout session export` の出力を CI シークレット "
            "JOBMEDLEY_STORAGE_STATE_B64 に登録してください。"
        )

    email, password = secrets.require_password_login()
    _fill_login_form(
        page,
        email=email,
        password=password,
        email_selector=email_selector,
        password_selector=password_selector,
        submit_selector=submit_selector,
        submit_text_candidates=submit_text_candidates,
        is_spa=is_spa,
        waits=waits,
    )

    if not wait_for_marker(page, marker, config):
        raise PermanentAuthError(
            "自動ログイン後にログイン成功マーカーが現れませんでした。"
            "2段階認証が挟まっているか、ボット検知に掛かった可能性があります "
            "(5.4: データセンターのIPからの自動ログインは失敗しやすい)。"
            "保存セッションの持ち込みに切り替えてください。"
        )

    session_store.save(context, credentials_dir)
    return AuthResult(AuthRoute.PASSWORD_LOGIN, True, "メール/パスワードで認証")


def _fill_login_form(
    page: Any,
    *,
    email: str,
    password: str,
    email_selector: Coord[str],
    password_selector: Coord[str],
    submit_selector: Coord[str],
    submit_text_candidates: Coord[tuple[str, ...]],
    is_spa: Coord[bool],
    waits: WaitsConfig,
) -> None:
    email_sel = require(email_selector, used_by="browser.auth._fill_login_form")
    password_sel = require(password_selector, used_by="browser.auth._fill_login_form")

    page.fill(email_sel, email)
    # 入力欄の間にも待機を入れる (5.2)。
    pause(waits.login_form_fields)
    page.fill(password_sel, password)
    pause(waits.login_form_fields)

    # 5.5: 送信ボタンのクリックにはフォールバックを用意する。
    if _try_click(page, submit_selector):
        return

    spa = is_resolved(is_spa) and require(is_spa, used_by="browser.auth._fill_login_form")
    if spa:
        # SPAのログイン画面では Enter がフォーム送信にならないことがあるので、
        # 複数のテキスト候補でボタンを探す (5.5)。
        if is_resolved(submit_text_candidates):
            candidates = require(submit_text_candidates, used_by="browser.auth._fill_login_form")
            for text in candidates:
                try:
                    page.get_by_role("button", name=text).click()
                    return
                except Exception:
                    continue
        raise PermanentAuthError(
            "SPAのログインフォームで送信ボタンを押せませんでした。"
            "座標 auth.submit_selector / auth.submit_text_candidates を見直してください。"
        )

    # 旧来型のフォームなら、パスワード欄で Enter が送信になる。
    page.press(password_sel, "Enter")


def _try_click(page: Any, selector: Coord[str]) -> bool:
    if not is_resolved(selector):
        return False
    try:
        page.click(require(selector, used_by="browser.auth._try_click"))
    except Exception:
        return False
    return True
