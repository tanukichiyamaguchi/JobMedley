"""Modal and drawer handling.

5.7 の事故:

> 参照実装では、候補者ごとにドロワーを開いてIDを取得する処理で、**2件目以降の
> クリックが開いたままのドロワーに遮られて失敗しました。**

対処 (この順序が仕様):

1. 閉じるコントロールを、**実画面で確認した順** に総当たりする
2. どれも押せなければ Escape キーを送る
3. **その後、要素が実際に消えたかを検証する**
4. 消えていなければ一覧ページへ再遷移して状態を強制的にリセットする

> N件をループで処理するDOM操作では、**1件ごとに画面状態を初期化できることを
> 保証しないと2件目で壊れます。**

閉じるコントロールの候補は座標 ``nav.drawer_close_selectors`` である。実画面を
見ないと分からないので、確定するまでは Escape と再遷移だけで凌ぐことになる。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jobmedley_scout.browser.navigation import goto, marker_present
from jobmedley_scout.config.schema import BrowserConfig


class CloseMethod(StrEnum):
    """How the overlay ended up closed. Recorded so degradation is visible."""

    CONTROL = "control"
    ESCAPE = "escape"
    RENAVIGATED = "renavigated"
    ALREADY_CLOSED = "already_closed"
    FAILED = "failed"


@dataclass(frozen=True)
class CloseOutcome:
    method: CloseMethod
    detail: str

    @property
    def succeeded(self) -> bool:
        return self.method is not CloseMethod.FAILED


def close_overlay(
    page: Any,
    *,
    overlay_selector: str | None,
    close_selectors: tuple[str, ...],
    list_url: str | None,
    list_ready_selector: str | None,
    config: BrowserConfig,
) -> CloseOutcome:
    """Close an overlay and **verify it is gone**.

    検証まで含めて1操作とする。「閉じるボタンを押した」で終わらせると、
    描画が間に合わないまま次のクリックへ進み、2件目が壊れる。
    """
    if overlay_selector is not None and not marker_present(page, overlay_selector, timeout_ms=500):
        return CloseOutcome(CloseMethod.ALREADY_CLOSED, "オーバーレイは既に閉じていた")

    # 1. 実画面で確認した順に総当たり。
    for selector in close_selectors:
        try:
            element = page.query_selector(selector)
            if element is None:
                continue
            element.click()
        except Exception:
            continue
        if _confirm_gone(page, overlay_selector):
            return CloseOutcome(CloseMethod.CONTROL, f"セレクタ {selector} で閉じた")

    # 2. どれも押せなければ Escape。
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    if _confirm_gone(page, overlay_selector):
        return CloseOutcome(CloseMethod.ESCAPE, "Escapeで閉じた")

    # 4. それでも消えなければ一覧へ再遷移して状態を強制リセット。
    if list_url is not None:
        goto(page, list_url, config)
        if list_ready_selector is not None:
            marker_present(page, list_ready_selector, timeout_ms=config.selector_timeout_ms)
        if _confirm_gone(page, overlay_selector):
            return CloseOutcome(CloseMethod.RENAVIGATED, "一覧へ再遷移して状態をリセットした")

    return CloseOutcome(
        CloseMethod.FAILED,
        "オーバーレイを閉じられませんでした。次の候補者のクリックが遮られるため、"
        "このバッチは中断すべきです (5.7)。",
    )


def _confirm_gone(page: Any, overlay_selector: str | None) -> bool:
    """**消えたことの検証。** これを省くと 5.7 の事故がそのまま再現する。"""
    if overlay_selector is None:
        # セレクタが未確定 (座標未記入)。消えたと断定できないので、
        # 「確認できなかった」= False を返す。楽観的に True を返してはいけない。
        return False
    return not marker_present(page, overlay_selector, timeout_ms=1500)
