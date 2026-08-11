"""Navigation and waiting.

5.3 の事故:

> 参照実装では、ページ読み込み完了の判定に「ネットワークがアイドルになるまで待つ」を
> 使い、**毎回30秒のタイムアウトを捨てていました。**
>
> 原因は、媒体サイトが計測タグとロングポーリングで常時通信しており、アイドル状態に
> 到達しないことでした。

対処:

* 待つ対象を「通信の静止」ではなく **「目的の要素の出現」** に変える
* ページ読み込みは短いタイムアウトを付けて例外を握りつぶす
* 成否の判定は **マーカー要素の存在** で行う

> 求人媒体は例外なく計測タグ・チャットウィジェット・通知ポーリングを積んでいます。
> **ネットワークアイドルは最初から当てにしないでください。**

本モジュールに ``networkidle`` を待つ経路は **存在しない**。
``tests/guardrails/test_no_networkidle.py`` が、その語がリポジトリ全体で
(ガードレールテスト自身を除いて) 出現しないことを検査している。
"""

from __future__ import annotations

import contextlib
from typing import Any

from jobmedley_scout.config.schema import BrowserConfig


def goto(page: Any, url: str, config: BrowserConfig) -> None:
    """Navigate, tolerating a load-event timeout.

    読み込み完了イベントを待ちはするが、**来なくても構わない**。本当に見たいのは
    目的の要素であって、通信が静まることではない。
    """
    # 5.3: 短いタイムアウトを付けて例外は握りつぶす。成否は要素で判定する。
    # ここで落とすと、計測タグが1本刺さっているだけで全処理が止まる。
    with contextlib.suppress(Exception):
        page.goto(url, wait_until="domcontentloaded", timeout=config.navigation_timeout_ms)


def wait_for_marker(page: Any, selector: str, config: BrowserConfig) -> bool:
    """Whether the marker element appeared. This is how success is decided."""
    try:
        page.wait_for_selector(selector, timeout=config.selector_timeout_ms, state="attached")
    except Exception:
        return False
    return True


def goto_and_wait_for(page: Any, url: str, selector: str, config: BrowserConfig) -> bool:
    """Navigate and confirm arrival by the marker element, not by the load event."""
    goto(page, url, config)
    return wait_for_marker(page, selector, config)


def marker_present(page: Any, selector: str, timeout_ms: int = 2000) -> bool:
    """A quick, non-blocking check for an element.

    ドロワーが実際に消えたかの検証 (5.7) など、「無いことを確かめたい」用途に使う。
    """
    try:
        page.wait_for_selector(selector, timeout=timeout_ms, state="attached")
    except Exception:
        return False
    return True
