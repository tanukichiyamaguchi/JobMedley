"""Stage 1's pass condition, checked.

3章 段階1:

> **合格条件**: 保存したセッションを読み込んで再起動し、ログイン状態が復元される。
>
> **撤退条件**: 復元できない場合、セッション持ち込み方式は使えない。

このモジュールはその判定だけを行う。**判定に失敗したときに「たぶん大丈夫」を
返さない** ことが要点である。復元できたか分からない状態を「できた」に丸めると、
段階2以降が「入れているつもりで0件」で進み、原則2の静かなゼロ件になる。

判定は二経路ある:

* ``auth.success_marker_selector`` が **確定済み** なら、そのセレクタで厳密に
  判定する (5.5 の本来の判定)
* **未確定** なら、ログアウト系リンクの有無というヒューリスティックで代用する。
  記入する前に「そもそも復元できるのか」を確かめたい場面があるため。
  **代用したことは結果に明示して返す** -- 黙って代用すると、運用者は厳密判定が
  行われたと読む

ヒューリスティックには第三の答え「判定できない」がある。ログアウトらしき文言と
パスワード欄が両方見えた場合や、どちらも見えなかった場合がそれで、**どちらかに
寄せて返さない**。分からないことを分かったことにするのが、この指示書全体が
禁じている推測そのものだからである。

ブラウザ依存部は薄く保ち、判定は純粋関数に置く (13.4)。
:func:`heuristic_verdict` はブラウザ無しでテストできる。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.browser.navigation import goto, marker_present
from jobmedley_scout.config.placeholders import Coord, is_resolved, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.known import LOGOUT_TEXT_HINTS, PUBLIC_SIGN_IN_URL

#: 描画済みDOMからパスワード欄を探すセレクタ。**ログイン画面に戻された** ことの
#: 手掛かりであり、:func:`recon.manual_login.login_form_present_in_html` とは
#: 目的が違う (あちらは素のHTMLを見てSPAかどうかを判定する)。
PASSWORD_FIELD_SELECTOR = 'input[type="password"]'


class Verdict(StrEnum):
    """What we concluded. ``INDETERMINATE`` is a real answer, not a failure mode."""

    RESTORED = "restored"
    NOT_RESTORED = "not_restored"
    #: 判定できなかった。**成功にも失敗にも寄せない。**
    INDETERMINATE = "indeterminate"
    #: そもそも保存セッションが無い。段階1をまだ実施していない。
    NO_SESSION = "no_session"


class VerifyMethod(StrEnum):
    """How the verdict was reached. Printed, because the two are not equivalent."""

    #: 座標 ``auth.success_marker_selector`` による厳密判定 (5.5)。
    MARKER = "marker"
    #: マーカー未確定のための代用判定。**合格条件の本来の判定ではない。**
    LOGOUT_HEURISTIC = "logout_heuristic"
    #: 判定に至らなかった (セッションが無い等)。
    NONE = "none"


# --- 純粋関数 (テスト可能) ---------------------------------------------------


def heuristic_verdict(*, logout_hits: tuple[str, ...], password_field_present: bool) -> Verdict:
    """The fallback verdict, from two independent observations.

    片方だけを見ないのが要点である。ログアウトらしき文言の有無だけで判定すると、
    公開ページのフッタに「ログアウト」の語がある媒体で常に成功になる。パスワード欄の
    有無だけで判定すると、SPAが未ログイン画面を遅れて描画する構成で常に成功になる。

    **両方見えた / どちらも見えなかった場合は :attr:`Verdict.INDETERMINATE`。**
    多数決も優先順位も付けない -- 「どちらとも言えない」を片側に丸めた瞬間、
    この関数は推測を返す関数になる。
    """
    if logout_hits and not password_field_present:
        return Verdict.RESTORED
    if password_field_present and not logout_hits:
        return Verdict.NOT_RESTORED
    return Verdict.INDETERMINATE


@dataclass(frozen=True)
class VerifyResult:
    """The verdict, plus everything the operator needs to act on it."""

    verdict: Verdict
    method: VerifyMethod
    landed_url: str
    session_path: Path
    logout_hits: tuple[str, ...] = ()
    password_field_present: bool = False

    @property
    def passed(self) -> bool:
        """**Only** :attr:`Verdict.RESTORED` passes. Indeterminate does not."""
        return self.verdict is Verdict.RESTORED

    def render(self) -> str:
        lines = ["段階1の合格条件の確認", ""]

        if self.verdict is Verdict.NO_SESSION:
            lines.extend(
                [
                    f"保存セッションがありません: {self.session_path}",
                    "",
                    "先に `scout recon login` をローカルで実行し、手動でログインしてください。",
                ]
            )
            return "\n".join(lines)

        lines.append(f"保存セッション: {self.session_path}")
        lines.append(f"到達したURL  : {self.landed_url}")
        lines.append("")

        if self.method is VerifyMethod.MARKER:
            lines.append("判定方法: 座標 auth.success_marker_selector による厳密判定 (5.5)")
        else:
            lines.extend(
                [
                    "判定方法: ログアウト系リンクによる **代用判定**",
                    "  座標 auth.success_marker_selector が未確定のため、厳密判定ができません。",
                    "  これは段階1の合格条件そのものではありません。マーカーを記入したうえで",
                    "  もう一度実行し、厳密判定で確認してください。",
                    f"  ログアウト系の文言: {list(self.logout_hits) or 'なし'}",
                    f"  パスワード入力欄  : {'あり' if self.password_field_present else 'なし'}",
                ]
            )
        lines.append("")

        if self.verdict is Verdict.RESTORED:
            lines.extend(
                [
                    "結果: ログイン状態が復元されました。",
                    "",
                    "次: `scout session export` の出力を CI のシークレット",
                    "    JOBMEDLEY_STORAGE_STATE_B64 に登録してください (5.4 経路1)。",
                ]
            )
        elif self.verdict is Verdict.NOT_RESTORED:
            lines.extend(
                [
                    "結果: ログイン状態が復元されませんでした。",
                    "",
                    "考えられること:",
                    "  - セッションの有効期限が切れた → `scout recon login` をやり直す",
                    "  - Cookie だけでは復元できない構成 (localStorage 等に依存)",
                    "    → **撤退条件に該当します。** セッション持ち込み方式が使えないため、",
                    "      設計の変更を利用者と相談してください (段階1の撤退条件)。",
                ]
            )
        else:
            lines.extend(
                [
                    "結果: **判定できませんでした。**",
                    "",
                    "ログアウト系の文言とパスワード欄の見え方が、どちらとも言えない状態です。",
                    "成功にも失敗にも寄せずに止めています。",
                    "",
                    "対処: 座標 auth.success_marker_selector を記入してから",
                    "      もう一度実行してください。厳密判定になれば曖昧さは消えます。",
                    "      ヘッドフル (config.yaml の browser.headless: false) で実行して",
                    "      画面を目で確認するのも有効です。",
                ]
            )
        return "\n".join(lines)


# --- ブラウザ依存部 (私は検証できない。運用者の実機確認に委ねる) ----------------


def _logout_hits(page: Any) -> tuple[str, ...]:
    """Logout-ish link/button labels visible on the page.

    ``page.content()`` の全文検索にしないのは、JSバンドルの中の文字列まで
    拾ってしまい、未ログインでも常に一致するようになるため。
    """
    hits: list[str] = []
    for tag in ("a", "button"):
        try:
            elements = page.query_selector_all(tag)
        except Exception:
            continue
        for element in elements:
            try:
                text = (element.inner_text() or "").strip()
            except Exception:
                continue
            for hint in LOGOUT_TEXT_HINTS:
                if hint in text and text not in hits:
                    hits.append(text)
    return tuple(hits)


def _password_field_present(page: Any) -> bool:
    try:
        return page.query_selector(PASSWORD_FIELD_SELECTOR) is not None
    except Exception:
        return False


def verify_saved_session(
    config: BrowserConfig,
    credentials_dir: Path,
    marker_selector: Coord[str],
) -> VerifyResult:
    """Restart with the saved session and see whether we are still logged in.

    遷移先は **公開のサインインURL** で固定してある。座標 ``auth.login_url`` を
    使わないのは、それが未確定でもこの確認を行えるようにするため -- 記入前に
    「そもそも復元できるのか」を確かめたい場面があり、そこで座標を要求すると
    段階1の中で循環が起きる。認証済みならサインイン画面から追い出されるので、
    確認先としてはこれで足りる。
    """
    path = session_store.session_path(credentials_dir)
    if not path.exists():
        return VerifyResult(
            verdict=Verdict.NO_SESSION,
            method=VerifyMethod.NONE,
            landed_url="",
            session_path=path,
        )

    with browser_context(config, storage_state=path) as (_context, page):
        goto(page, PUBLIC_SIGN_IN_URL, config)

        if is_resolved(marker_selector):
            marker = require(marker_selector, used_by="recon.verify_session.verify_saved_session")
            found = marker_present(page, marker, timeout_ms=config.selector_timeout_ms)
            return VerifyResult(
                verdict=Verdict.RESTORED if found else Verdict.NOT_RESTORED,
                method=VerifyMethod.MARKER,
                landed_url=page.url,
                session_path=path,
            )

        hits = _logout_hits(page)
        has_password = _password_field_present(page)
        return VerifyResult(
            verdict=heuristic_verdict(logout_hits=hits, password_field_present=has_password),
            method=VerifyMethod.LOGOUT_HEURISTIC,
            landed_url=page.url,
            session_path=path,
            logout_hits=hits,
            password_field_present=has_password,
        )
