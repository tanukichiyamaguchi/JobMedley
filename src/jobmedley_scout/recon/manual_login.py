"""Stage 1: a human logs in by hand, and we keep the result.

3章 段階1:

> ヘッドフルモードで起動し、人間が手動でログインする (2段階認証もここで突破する)。
> ログイン状態をファイルに保存する。
> **合格条件**: 保存したセッションを読み込んで再起動し、ログイン状態が復元される。

**この経路は :func:`browser.auth.ensure_authenticated` を通さない。** あちらは
ログイン成功マーカーのセレクタを ``require()`` するが、**そのマーカーこそ段階1の
成果物** だからである。段階1の入力に段階1の出力を要求すると、ラダーの1歩目が
始められなくなる。

人間の待ち方も同じ理屈で決めてある。マーカーが未確定なので「ログインできたか」を
機械が判定できない -- だから判定しない。**人間が終わったと言うまで待つ。**
2段階認証がSMSでもメールリンクでも、待ち方は変わらない。

観測結果は座標キー名に紐づけて印字する (:mod:`recon.resume_keys` と同じ方針)。
運用者は ``config/site_coordinates.yaml`` へ転記するだけでよい。
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.recon.known import LOGOUT_TEXT_HINTS, PUBLIC_SIGN_IN_URL

_PASSWORD_INPUT = re.compile(r"""<input[^>]*type\s*=\s*["']?password["']?""", re.IGNORECASE)
#: CSS識別子として安全に使えるクラス名。ハッシュ的なものは避ける
#: (ビルドのたびに変わるので、セレクタとして書くと次のデプロイで壊れる)。
_STABLE_CLASS = re.compile(r"^[a-zA-Z][\w-]{2,}$")
_HASHY = re.compile(r"[0-9a-f]{6,}", re.IGNORECASE)


# --- 純粋関数 (テスト可能) ---------------------------------------------------


def login_form_present_in_html(html: str) -> bool:
    """Whether the *served* HTML already contains the login form.

    座標 ``auth.is_spa`` の判定材料。指示書の言う「右クリック→ページのソースを表示」
    を機械化したもの: パスワード入力欄が素のHTMLに含まれていれば旧来型、
    JavaScript で描画されるなら含まれない。

    **JSを実行していないHTMLに対して呼ぶこと。** ブラウザで描画済みのDOMを渡すと
    SPAでもフォームが見つかり、判定が常に「旧来型」になる。
    """
    return bool(_PASSWORD_INPUT.search(html))


def marker_selector_candidates(
    tag: str, element_id: str | None, class_names: tuple[str, ...], text: str
) -> tuple[str, ...]:
    """Candidate selectors for one logout-ish element, most stable first.

    座標 ``auth.success_marker_selector`` の記入を助けるためのもの。
    id > クラス > テキスト の順に並べるのは、その順に画面変更へ強いから。

    ハッシュめいたクラス名 (``css-1a2b3c4``) は候補から外す -- ビルドのたびに
    変わるので、セレクタとして書くと次のデプロイで静かに壊れる。5.5 が
    「セレクタは設定ファイルで上書きできるようにし、画面変更時にコードを触らずに
    追従できるように」と言っているのは、壊れる前提だからである。
    """
    candidates: list[str] = []
    if element_id and _STABLE_CLASS.match(element_id) and not _HASHY.search(element_id):
        candidates.append(f"#{element_id}")
    for name in class_names:
        if _STABLE_CLASS.match(name) and not _HASHY.search(name):
            candidates.append(f"{tag}.{name}")
    stripped = text.strip()
    if stripped:
        candidates.append(f'{tag}:has-text("{stripped}")')
    return tuple(dict.fromkeys(candidates))


def form_field_selector_candidates(
    tag: str, element_id: str | None, name: str | None, type_attr: str | None
) -> tuple[str, ...]:
    """Candidate selectors for one login-form field, most stable first.

    座標 ``auth.email_selector`` / ``auth.password_selector`` /
    ``auth.submit_selector`` の記入を助ける。

    ``id`` > ``name`` 属性 > ``type`` 属性 の順。``name`` を ``type`` より上に
    置くのは、``type`` がフォーム内で一意とは限らないから (``input[type="text"]``
    がメール欄と検索欄の両方に一致する画面は珍しくない)。``name`` はサーバへ送る
    キーそのものなので、画面の作り替えより変わりにくい。

    :func:`marker_selector_candidates` と同じく、ハッシュめいた ``id`` は捨てる --
    ビルドのたびに変わるので、書いた瞬間から壊れる予定のセレクタになる。
    """
    candidates: list[str] = []
    if element_id and _STABLE_CLASS.match(element_id) and not _HASHY.search(element_id):
        candidates.append(f"#{element_id}")
    if name:
        # 属性値は引用符で囲む。``customer[email]`` のような角括弧を含む name が
        # 珍しくなく、囲まないとCSSセレクタとして壊れる。
        candidates.append(f'{tag}[name="{name}"]')
    if type_attr:
        candidates.append(f'{tag}[type="{type_attr}"]')
    return tuple(dict.fromkeys(candidates))


def headful_display_available(platform: str, env: Mapping[str, str]) -> bool:
    """Whether a human could actually see a browser window here.

    クラウドの実行環境でこのコマンドを呼ぶと、Playwright の起動失敗という
    **原因の分かりにくい形** で落ちる。画面が無いことは事前に分かるので、
    先に判定して「代わりに何をすればよいか」を言う。

    macOS と Windows には ``DISPLAY`` の概念が無いので、Linux のときだけ見る。
    無い環境で無条件に止めると、手元のPCで実行できるはずの人まで止めてしまう。
    """
    if not platform.startswith("linux"):
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


NO_DISPLAY_MESSAGE = (
    "画面が無い環境では手動ログインできません (ブラウザを開いても見る人がいません)。\n"
    "  クラウドで作業している場合は、普通のブラウザから持ち込む経路を使ってください:\n"
    "    1. 普段のブラウザで媒体にログインする (何もインストールしません)\n"
    "    2. 開発者ツール → Network → 認証済みリクエストを右クリック → Copy as cURL\n"
    "    3. その文字列を GitHub のシークレット JOBMEDLEY_SESSION_CURL に登録する\n"
    "    4. Actions の 'Recon (manual)' から verify-session を実行する\n"
    "  手順の全文は docs/ladder.md 段階1にあります。"
)


@dataclass(frozen=True)
class MarkerCandidate:
    text: str
    selectors: tuple[str, ...]


@dataclass(frozen=True)
class LoginObservation:
    """What stage 1 learned. Every field maps to a coordinate."""

    landed_url: str
    login_form_in_served_html: bool
    marker_candidates: tuple[MarkerCandidate, ...]
    session_path: Path

    def render(self) -> str:
        lines = [
            "段階1の観測結果",
            "",
            f"セッションを保存しました: {self.session_path}",
            "",
            "config/site_coordinates.yaml へ転記してください:",
            "",
            f"  auth.login_url: {self.landed_url}",
            f"  auth.is_spa: {str(not self.login_form_in_served_html).lower()}",
        ]
        if self.login_form_in_served_html:
            lines.append("    ^ 素のHTMLにパスワード欄がある = 旧来型のサーバレンダリング。")
            lines.append(
                "      セレクタの外出しが素直に効き、パスワード欄で Enter が送信になります。"
            )
        else:
            lines.append("    ^ 素のHTMLにパスワード欄が無い = SPA の可能性が高い。")
            lines.append(
                "      SPAでは Enter がフォーム送信にならないことがあるため、"
                "auth.submit_text_candidates も埋めてください (5.5)。"
            )

        lines.extend(["", "  auth.success_marker_selector: 下の候補から1つ選ぶ"])
        if self.marker_candidates:
            for candidate in self.marker_candidates:
                lines.append(f"    「{candidate.text}」")
                lines.extend(f"      {selector}" for selector in candidate.selectors)
            lines.append("")
            lines.append(
                "    上ほど画面変更に強い候補です (id > クラス > テキスト)。"
                "**遷移の完了やステータスコードで判定してはいけません** (5.5)。"
            )
        else:
            lines.append("    候補が見つかりませんでした。")
            lines.append(
                "    ログイン後にのみ存在する要素を開発者ツールで探してください。"
                "ログアウトリンク以外でも構いません (アカウント名の表示など)。"
            )

        lines.extend(
            [
                "",
                "残りの段階1の座標 (自動ログインのフォールバック用) は、"
                "ログインフォームを開発者ツールで検査して埋めてください:",
                "  auth.email_selector / auth.password_selector /"
                " auth.submit_selector / auth.submit_text_candidates",
                "",
                "2段階認証の種別も記録してください: auth.twofa_kind",
                "  (none / sms / totp / email_link)",
                "  email_link の場合、CIで突破する手段が無いため、"
                "この保存セッションの持ち込みが必須になります (4章・5.4)。",
                "",
                "次: `scout recon verify-session` で合格条件を確認してください。",
            ]
        )
        return "\n".join(lines)


# --- ブラウザ依存部 (私は検証できない。運用者の実機確認に委ねる) ----------------


def collect_marker_candidates(page: Any) -> tuple[MarkerCandidate, ...]:
    found: list[MarkerCandidate] = []
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
            if not any(hint in text for hint in LOGOUT_TEXT_HINTS):
                continue
            try:
                element_id = element.get_attribute("id")
                class_attr = element.get_attribute("class") or ""
            except Exception:
                element_id, class_attr = None, ""
            selectors = marker_selector_candidates(tag, element_id, tuple(class_attr.split()), text)
            if selectors:
                found.append(MarkerCandidate(text=text, selectors=selectors))
    return tuple(found)


def run_manual_login(
    config: BrowserConfig,
    credentials_dir: Path,
    *,
    wait_for_human: Any = input,
) -> LoginObservation:
    """Open a real browser, let a human log in, then keep the session.

    ``wait_for_human`` を差し替え可能にしてあるのは、テストのためではなく
    (ブラウザごとテストできないので) 非対話環境で誤って起動したときに
    ハングさせないため。
    """
    # 画面が無いなら、ブラウザを起動する前に止める。起動してから Playwright の
    # エラーで落ちると、原因が「画面が無いこと」だと分からない。
    if not headful_display_available(sys.platform, os.environ):
        raise ConfigError(NO_DISPLAY_MESSAGE)

    # ヘッドフルで開く。設定は headless=true が既定なので、ここだけ上書きする。
    # 人間が操作するので、これは譲れない。
    headful = config.model_copy(update={"headless": False})

    with browser_context(headful, storage_state=None) as (context, page):
        # 素のHTML (JS実行前) を取得して SPA 判定の材料にする。
        # 描画済みDOMを見ると SPA でもフォームが見つかってしまう。
        served_html = ""
        try:
            response = context.request.get(PUBLIC_SIGN_IN_URL)
            served_html = response.text()
        except Exception:
            # 取得できなくても段階1は続行できる。is_spa は人間が埋めればよい。
            served_html = ""

        page.goto(PUBLIC_SIGN_IN_URL)

        print("=" * 70)
        print("ブラウザを開きました。画面上で普通にログインしてください。")
        print("2段階認証もこの画面で突破してください。")
        print("")
        print("ログインが完了したら、このターミナルで Enter を押してください。")
        print("(ログイン成功マーカーはまだ未確定なので、機械では判定できません --")
        print(" だから判定せず、あなたの合図を待ちます)")
        print("=" * 70)
        wait_for_human()

        landed_url = page.url
        candidates = collect_marker_candidates(page)
        # 5.4: **ログイン成功直後に保存する。** 終了時だけに任せると、途中で
        # 落ちたときに手動ログインをやり直す羽目になる。
        session_path = session_store.save(context, credentials_dir)

    return LoginObservation(
        landed_url=landed_url,
        login_form_in_served_html=login_form_present_in_html(served_html),
        marker_candidates=candidates,
        session_path=session_path,
    )
