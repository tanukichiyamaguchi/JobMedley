"""Observe stage 1's coordinates instead of asking a human to read them.

段階1の座標は8個ある。手順書は当初、その全部を運用者が開発者ツールで読み取って
``config/site_coordinates.yaml`` へ転記する前提だった。**それは不要な負担であり、
かつ間違いやすい。** 読み間違えたセレクタは検証されないまま YAML に入り、実行時に
静かに空振りする。

しかし8個のうち **7個は観測できる**。認証済みセッションは既にシークレットから
復元されており、未認証のブラウザも同じ実行の中で開けるからである。

観測できない1個は ``auth.twofa_kind``。実際に何が出たかは、その場でログインした
人間しか知らない。**観測できないものを観測したことにしない** ので、これは空欄の
まま人間の記入に委ねる (原則3)。

なぜ2つのコンテキストを開くのか
-------------------------------

**認証済みのままではログインフォームを観測できない。** サインインURLを開いても
ログイン済みなら追い出されるので、フォームは描画されない。逆にログアウトリンクは
認証済みでなければ存在しない。両方を1回の実行で得るには、未認証と認証済みの
コンテキストを別々に開くしかない。

順序は「未認証 → 認証済み」。逆にすると、認証済みコンテキストを閉じてから未認証を
開くことになり、失敗したときにどちらの観測が欠けたのか分かりにくくなる。

判定ロジックは :mod:`recon.manual_login` の純粋関数に置いてある (13.4)。本モジュールは
それらへ値を運ぶだけで、**判断はしない**。
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.browser.dom import (
    PASSWORD_INPUT,
    clickables,
    login_form_visible,
    page_title,
    wait_for_interactive,
)
from jobmedley_scout.browser.navigation import goto, marker_present
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.known import PUBLIC_SIGN_IN_URL
from jobmedley_scout.recon.manual_login import (
    MarkerCandidate,
    form_field_selector_candidates,
    login_form_present_in_html,
    marker_candidates_from,
    structure_sample,
)
from jobmedley_scout.recon.yaml_paste import yaml_scalar as _scalar

#: 送信ボタンらしき要素を探す順。上ほど確実。
_SUBMIT_SELECTORS: tuple[str, ...] = (
    'button[type="submit"]',
    'input[type="submit"]',
    "form button",
    "button",
)


@dataclass(frozen=True)
class ObservedLogin:
    """Everything stage 1 could observe. Fields map 1:1 to coordinates."""

    login_url: str
    login_form_in_served_html: bool
    email_selectors: tuple[str, ...]
    password_selectors: tuple[str, ...]
    submit_selectors: tuple[str, ...]
    submit_texts: tuple[str, ...]
    marker_candidates: tuple[MarkerCandidate, ...]
    #: 保存セッションのファイルが存在したか。**「認証できた」ではない。**
    session_present: bool
    #: 認証済みコンテキストが実際に到達したURL。空なら走査していない。
    authenticated_url: str
    #: その画面にパスワード欄があったか。**あるならセッションが効いていない。**
    authenticated_login_form_visible: bool
    #: 認証済みの画面のタイトル。どの画面に居たのかの証拠。
    authenticated_title: str = ""
    #: その画面にあったリンク/ボタンの **構造** (``a.c-header-menu__logout-link`` 等)。
    #: **文言は出さない** (13.2)。無かったものではなく有ったものを出しつつ、
    #: 個人データを実行ログへ流さないための形。
    authenticated_structure: tuple[str, ...] = ()

    @property
    def authenticated_observation(self) -> bool:
        """Whether we can believe the marker scan looked at a logged-in page.

        以前はここが「セッションファイルがあった」だけで真になっていた。その値で
        「ログイン後にのみ存在する要素が見つかりませんでした」と印字していたので、
        **セッションが失効してサインイン画面に戻されていた場合でも、出力は
        1バイト違わず同じ** になった。媒体について何も分かっていないのに、
        媒体についての事実を述べていたことになる。

        パスワード欄が見えているなら、それは「マーカーが無い画面」ではなく
        「ログインしていない画面」である。
        """
        return self.session_present and not self.authenticated_login_form_visible

    def _evidence_lines(self) -> list[str]:
        """What was actually on the authenticated page.

        **「無かったもの」だけでは切り分けられない。** 到達URLがサインインURLのまま、
        ログアウトリンクもパスワード欄も無い、という報告が実際に出た。3つとも
        「無い」ので、どの画面を見ていたのか誰にも分からなかった。有ったものを
        出せば、ログイン画面なのか、ログイン後の画面なのか、空なのかが一目で分かる。
        """
        lines = [f"    # 到達URL  : {self.authenticated_url or '(記録なし)'}"]
        if self.authenticated_title:
            lines.append(f"    # ページ名 : {self.authenticated_title}")
        if self.authenticated_structure:
            lines.append(
                f"    # 画面にあったリンク/ボタンの構造 ({len(self.authenticated_structure)}種):"
            )
            lines.extend(f"    #   - {token}" for token in self.authenticated_structure)
            lines.append("    #   (文言は出しません。個人データを実行ログに残さないため 13.2)")
        else:
            lines.append("    # 画面にリンクもボタンもありませんでした。")
            lines.append("    #   描画前か、真っ白な画面に着いています。")
        return lines

    def _yaml_line(self, key: str, candidates: tuple[str, ...]) -> list[str]:
        """One coordinate as a paste-ready line, with the alternatives beneath it.

        先頭候補だけを出して残りを捨てない。**選ぶのは人間** なので、選択肢を
        奪わない。先頭が最も安定しているという並び順は
        :func:`~recon.manual_login.form_field_selector_candidates` が保証する。
        """
        if not candidates:
            return [
                f"  {key}: {UNRESOLVED_TOKEN}",
                "    # 観測できませんでした。画面を開いて手で調べてください。",
            ]
        lines = [f"  {key}: {_scalar(candidates[0])}"]
        lines.extend(f"    # 別案: {alternative}" for alternative in candidates[1:])
        return lines

    def render(self) -> str:
        lines = [
            "段階1の観測結果",
            "",
            "config/site_coordinates.yaml の該当行を、下記で置き換えてください。",
            "**値は観測されたものであり、推測ではありません。**",
            "",
            f"  auth.login_url: {_scalar(self.login_url)}",
            f"  auth.is_spa: {str(not self.login_form_in_served_html).lower()}",
        ]
        if self.login_form_in_served_html:
            lines.append("    # 素のHTMLにパスワード欄あり = 旧来型のサーバレンダリング")
        else:
            lines.append("    # 素のHTMLにパスワード欄なし = SPA の可能性が高い")
            lines.append("    # SPAでは Enter がフォーム送信にならないことがあります (5.5)")

        lines.extend(self._yaml_line("auth.email_selector", self.email_selectors))
        lines.extend(self._yaml_line("auth.password_selector", self.password_selectors))
        lines.extend(self._yaml_line("auth.submit_selector", self.submit_selectors))

        if self.submit_texts:
            lines.append(f"  auth.submit_text_candidates: {_scalar(list(self.submit_texts))}")
        else:
            lines.append(f"  auth.submit_text_candidates: {UNRESOLVED_TOKEN}")
            lines.append("    # 送信ボタンの表示文字を読み取れませんでした。")

        lines.append("")
        if not self.session_present:
            lines.extend(
                [
                    f"  auth.success_marker_selector: {UNRESOLVED_TOKEN}",
                    "    # 保存セッションがありません。シークレット JOBMEDLEY_SESSION_CURL",
                    "    # (または JOBMEDLEY_STORAGE_STATE_B64) を設定してください。",
                ]
            )
        elif self.authenticated_login_form_visible:
            # **ここを「マーカーが無い」と報告してはいけない。** パスワード欄が
            # 見えているなら、見ていたのはログイン後の画面ではない。媒体について
            # 何も分かっていない状態で、媒体についての事実を述べることになる。
            lines.extend(
                [
                    f"  auth.success_marker_selector: {UNRESOLVED_TOKEN}",
                    "    # **セッションが効いていません。** 認証済みで開いたはずの画面に",
                    "    # パスワード欄がありました。",
                    "    # マーカーが存在しないのではなく、ログイン後の画面を見ていません。",
                    *self._evidence_lines(),
                    "    # セッションを取り直してから、もう一度実行してください",
                    "    # (docs/ladder.md「セッションが切れたときの取り直し方」)。",
                ]
            )
        elif self.marker_candidates:
            best = self.marker_candidates[0]
            lines.append(f"  auth.success_marker_selector: {_scalar(best.selectors[0])}")
            lines.append(f"    # 「{best.text}」に一致します")
            for candidate in self.marker_candidates:
                for selector in candidate.selectors:
                    if selector != best.selectors[0]:
                        lines.append(f"    # 別案: {selector}  (「{candidate.text}」)")
            lines.append(
                "    # 上ほど画面変更に強い順です (id > クラス > テキスト)。"
                "**遷移の完了やステータスコードで判定してはいけません** (5.5)。"
            )
        else:
            lines.extend(
                [
                    f"  auth.success_marker_selector: {UNRESOLVED_TOKEN}",
                    "    # ログアウト系の要素が見つかりませんでした。",
                    *self._evidence_lines(),
                    "    # ログアウトリンク以外でも構いません (アカウント名の表示など)。",
                    "    # 上の一覧に使えそうな見出しがあれば、それを教えてください。",
                ]
            )

        lines.extend(
            [
                "",
                # **注記は必ずコメント行に置く。** 値の右へ書くと、その文言まで値の
                # 一部として読まれる ("UNRESOLVED ← ここだけは..." という座標になる)。
                "    # ↓ ここだけは観測できません。**自分で書いてください。**",
                f"  auth.twofa_kind: {UNRESOLVED_TOKEN}",
                "    # none / sms / totp / email_link のいずれか。",
                "    # 実際に何が出たかは、その場でログインしたあなたしか知りません。",
                "    # 観測できないものを観測したことにはしないので、空欄にしてあります。",
                "    # email_link の場合、CIで突破する手段が無いため、いま使っている",
                "    # セッション持ち込みが恒久的に必須になります (4章・5.4)。",
            ]
        )
        return "\n".join(
            [
                *lines,
                "",
                "記入したら `scout recon verify-session` をもう一度実行してください。",
                "マーカーが埋まっていれば **厳密判定** になり、段階1が正式に完了します。",
            ]
        )

    def yaml_block(self) -> str:
        """Only the lines meant to be pasted. **Must parse as YAML.**

        散文と混ぜたまま検査すると、値の右に注記を書いてしまう事故
        (``UNRESOLVED  ← ここだけは自分で書いてください`` がまるごと値になる) や、
        引用符の入れ子で壊れる事故を見逃す。貼る部分だけを取り出せる形にして、
        テストで実際に読み込ませる。
        """
        return "\n".join(
            line for line in self.render().splitlines() if line.startswith("  ") and line.strip()
        )


# --- ブラウザ依存部 (私は検証できない。運用者の実機確認に委ねる) ----------------


def _attributes(element: Any) -> tuple[str | None, str | None, str | None]:
    try:
        return (
            element.get_attribute("id"),
            element.get_attribute("name"),
            element.get_attribute("type"),
        )
    except Exception:
        return (None, None, None)


def _field_selectors(page: Any, css: str, tag: str) -> tuple[str, ...]:
    try:
        element = page.query_selector(css)
    except Exception:
        return ()
    if element is None:
        return ()
    element_id, name, type_attr = _attributes(element)
    return form_field_selector_candidates(tag, element_id, name, type_attr)


def _email_selectors(page: Any) -> tuple[str, ...]:
    """The email field. Tries the typed input first, then a text input.

    ``type="email"`` を先に見るのは、``type="text"`` が検索欄など無関係な入力欄にも
    一致するため。見つからないときだけ緩める。
    """
    for css in ('input[type="email"]', 'form input[type="text"]', 'input[type="text"]'):
        selectors = _field_selectors(page, css, "input")
        if selectors:
            return selectors
    return ()


def _submit_observation(page: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Selectors for the submit control, and the text shown on it."""
    for css in _SUBMIT_SELECTORS:
        try:
            element = page.query_selector(css)
        except Exception:
            continue
        if element is None:
            continue
        element_id, name, type_attr = _attributes(element)
        tag = "input" if css.startswith("input") else "button"
        selectors = form_field_selector_candidates(tag, element_id, name, type_attr)
        # ``<button>`` は表示文字が内側のテキスト、``<input type="submit">`` は
        # value 属性。どちらか一方だけを見ると、片方の作りの画面で候補が空になる。
        texts: list[str] = []
        with contextlib.suppress(Exception):
            texts.append((element.inner_text() or "").strip())
        with contextlib.suppress(Exception):
            texts.append((element.get_attribute("value") or "").strip())
        texts = list(dict.fromkeys(text for text in texts if text))
        if selectors or texts:
            return tuple(selectors), tuple(texts)
    return (), ()


def observe_login(config: BrowserConfig, credentials_dir: Path) -> ObservedLogin:
    """Open both an anonymous and an authenticated browser, and record what is there."""
    # --- 未認証: ログインフォームはここでしか見られない ----------------------
    with browser_context(config, storage_state=None) as (context, page):
        served_html = ""
        try:
            # 素のHTML (JS実行前)。描画済みDOMを見ると SPA でもフォームが
            # 見つかってしまい、is_spa の判定が常に false になる。
            served_html = context.request.get(PUBLIC_SIGN_IN_URL).text()
        except Exception:
            served_html = ""

        goto(page, PUBLIC_SIGN_IN_URL, config)
        # **フォームの描画を待ってから読む。** SPA (``auth.is_spa: true``) では
        # ``domcontentloaded`` の時点でフォームがまだ無い。待たずに読むと、
        # セレクタ3種が揃って UNRESOLVED になる -- 「観測したが無かった」に
        # 見えるが、実際には「早く見すぎた」である。
        # 待つ対象は HTML の入力種別であって媒体固有の座標ではないので、
        # 探しているものを待つ循環にはならない。
        marker_present(page, PASSWORD_INPUT, timeout_ms=config.selector_timeout_ms)
        login_url = page.url
        email_selectors = _email_selectors(page)
        password_selectors = _field_selectors(page, PASSWORD_INPUT, "input")
        submit_selectors, submit_texts = _submit_observation(page)

    # --- 認証済み: ログアウトリンクはここでしか見られない --------------------
    session = session_store.session_path(credentials_dir)
    marker_candidates: tuple[MarkerCandidate, ...] = ()
    authenticated_url = ""
    authenticated_login_form = False
    authenticated_title = ""
    authenticated_structure: tuple[str, ...] = ()
    session_present = session.exists()
    if session_present:
        with browser_context(config, storage_state=session) as (_context, page):
            # 認証済みならサインインURLから追い出される。追い出された先に
            # ログアウトリンクがある、というのが verify-session で確認済みの動き。
            goto(page, PUBLIC_SIGN_IN_URL, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            found = clickables(page)
            marker_candidates = marker_candidates_from(found)
            authenticated_title = page_title(page)
            authenticated_structure = structure_sample(found)
            # **どこに着いたのかを必ず記録する。** これを取らなかったせいで、
            # 「マーカーが無い」のか「そもそも認証済みの画面に居ない」のかが
            # 区別できない報告が出た。区別できない報告は、原則2の静かなゼロ件が
            # 座標の欠落として定着する経路になる。
            authenticated_url = page.url
            # **マーカーが見つかったなら、それ自体がログイン済みの証拠** なので
            # 問い合わせない。この確認は待つAPIで行い、ログイン済みの画面では
            # 必ず満了する (パスワード欄は永遠に現れない)。払う価値があるのは、
            # 「マーカーが無い」の理由を切り分けるときだけである。
            authenticated_login_form = not marker_candidates and login_form_visible(
                page, config.selector_timeout_ms
            )

    return ObservedLogin(
        login_url=login_url,
        login_form_in_served_html=login_form_present_in_html(served_html),
        email_selectors=email_selectors,
        password_selectors=password_selectors,
        submit_selectors=submit_selectors,
        submit_texts=submit_texts,
        marker_candidates=marker_candidates,
        authenticated_title=authenticated_title,
        authenticated_structure=authenticated_structure,
        session_present=session_present,
        authenticated_url=authenticated_url,
        authenticated_login_form_visible=authenticated_login_form,
    )
