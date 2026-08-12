"""Read the DOM once, as plain data.

このモジュールが存在する理由は、実際に起きた誤診である。

``scout recon verify-session`` は認証済みのページで「ログアウト」を見つけたのに、
数分後の ``scout recon observe-login`` は同じセッションで **何も見つけなかった**。
両者の走査ロジックは同じ DOM に対しては同じ答えを返すので、差は DOM の側にあった。

原因は2つ重なっている。

1. **``query_selector_all`` は待たない。** 自動待機するのは ``wait_for_selector`` と
   ロケータAPIだけで、``context.set_default_timeout`` はこの呼び出しに効かない。
   SPA (``auth.is_spa: true``) では ``domcontentloaded`` の時点でヘッダがまだ
   描画されていないことがあり、そこを覗くと空に見える
2. **要素ハンドルは再描画で無効になる。** 従来の走査は1要素につき
   ``inner_text`` / ``id`` / ``class`` と3回ブラウザへ往復しており、その途中で
   ハイドレーションがヘッダを差し替えると、握っていたハンドルが無効化される。
   例外は握りつぶされるので、要素は **黙って** 候補から消える。往復1回で済む
   走査より、構造的に取りこぼしやすかった

対処は、**1回の ``evaluate`` で必要な属性をまとめて取り出し、以降はただのデータとして
扱う** こと。ハンドルを持ち歩かないので無効化されようがなく、判定は純粋関数へ移せる
(13.4)。速度も往復回数に比例して改善する。

**ここで待つのは「要素」であって「通信の静止」ではない** (5.3)。待つ対象は
``a, button`` という汎用の目印であり、媒体固有のセレクタではない -- 探している
マーカーそのものを待つのは循環参照になる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: クリック可能な要素をまとめて取り出す。**1回の往復で完結させる。**
#: ``innerText`` は表示されていない要素では ``textContent`` に落ちる (HTML仕様) ので、
#: 折り畳まれたメニューの中のリンクも読める。
_CLICKABLE_SCRIPT = """
() => Array.from(document.querySelectorAll('a, button')).map((el) => ({
  tag: el.tagName.toLowerCase(),
  id: el.id || null,
  classes: typeof el.className === 'string'
    ? el.className.split(/\\s+/).filter(Boolean)
    : [],
  text: (el.innerText || el.textContent || '').trim(),
}))
"""

#: 走査の前に出現を待つ汎用の目印。**媒体固有のセレクタではない。**
INTERACTIVE_SELECTOR = "a, button"


@dataclass(frozen=True)
class Clickable:
    """One anchor or button, as plain data. No browser handle is retained."""

    tag: str
    element_id: str | None
    class_names: tuple[str, ...]
    text: str


def wait_for_interactive(page: Any, timeout_ms: int) -> bool:
    """Wait until the page has rendered something clickable.

    SPA が描画を終える前に走査すると、空の DOM を「要素が存在しない」と読んでしまう。
    かといってマーカーそのものを待つことはできない -- それが探している当のものだから。
    そこで **汎用の目印** の出現を待つ。

    見つからなくても例外にしない。目印が本当に無いページ (エラーページなど) でも
    走査は続行し、「見つからなかった」として報告する方が、原因の切り分けに役立つ。
    """
    try:
        page.wait_for_selector(INTERACTIVE_SELECTOR, timeout=timeout_ms, state="attached")
    except Exception:
        return False
    return True


def clickables(page: Any) -> tuple[Clickable, ...]:
    """Every anchor and button on the page, read in a single round trip."""
    try:
        raw = page.evaluate(_CLICKABLE_SCRIPT)
    except Exception:
        return ()
    found: list[Clickable] = []
    for item in raw or ():
        try:
            found.append(
                Clickable(
                    tag=str(item.get("tag", "")),
                    element_id=item.get("id") or None,
                    class_names=tuple(str(name) for name in item.get("classes") or ()),
                    text=str(item.get("text") or "").strip(),
                )
            )
        except AttributeError:  # pragma: no cover - defensive
            continue
    return tuple(found)


def login_form_visible(page: Any) -> bool:
    """Whether a password field is on the *rendered* page.

    「ログイン画面に戻された」ことの手掛かり。認証済みのつもりで走査した結果が
    空だったとき、これが真なら **セッションが効いていない** のであって、
    「マーカーが存在しない」のではない。この2つを取り違えると、原則2の
    「静かなゼロ件」がそのまま座標の欠落として定着する。
    """
    try:
        return page.query_selector('input[type="password"]') is not None
    except Exception:
        return False
