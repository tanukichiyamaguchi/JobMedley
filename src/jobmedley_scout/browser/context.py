"""The single browser-context factory.

5.1: この設定は **1箇所に集約し、すべてのコマンドが同じコンテキスト生成を通る**
ようにすること。偵察だけ指紋が違う、という状態を作らないため。

指定するもの:

* 自動化フラグの無効化 (``--disable-blink-features=AutomationControlled``)
* User-Agent -- **ハードコードせず必ず設定値** (メジャーバージョンが古くなりすぎると
  かえって不自然になるため、更新できる必要がある)
* ロケール ``ja-JP`` / タイムゾーン ``Asia/Tokyo`` / ビューポート 1366x900
* ``Accept-Language`` ヘッダ

本モジュールは Playwright に依存するため、``mypy`` の strict 設定が
``jobmedley_scout.browser.*`` だけ緩められている。**判定ロジックをここへ置かない
こと** -- ブラウザ依存部は薄く保ち、決定は純粋関数へ追い出す (13.4)。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jobmedley_scout.config.schema import BrowserConfig

#: 自動化されたブラウザであることを示す痕跡を消す起動引数 (5.1)。
LAUNCH_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
)


def context_options(config: BrowserConfig, storage_state: Path | None) -> dict[str, Any]:
    """Build the context kwargs. Pure -- so the fingerprint can be unit-tested.

    ブラウザを起動せずに「UAが設定から来ているか」「ロケールが ja-JP か」を
    検証できるよう、辞書を組み立てるところだけ分離してある。
    """
    options: dict[str, Any] = {
        "user_agent": config.user_agent,
        "locale": config.locale,
        "timezone_id": config.timezone,
        "viewport": {"width": config.viewport_width, "height": config.viewport_height},
        "extra_http_headers": {"Accept-Language": config.accept_language},
    }
    if storage_state is not None and storage_state.exists():
        options["storage_state"] = str(storage_state)
    return options


@contextmanager
def browser_context(
    config: BrowserConfig, storage_state: Path | None = None
) -> Iterator[tuple[Any, Any]]:
    """Yield ``(context, page)`` with the standard fingerprint applied.

    Playwright の import はここでのみ行う。api/ や targeting/ がブラウザ抜きで
    import できる状態を保つため (契約テストがブラウザを起動しないのはこのおかげ)。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless, args=list(LAUNCH_ARGS))
        try:
            context = browser.new_context(**context_options(config, storage_state))
            context.set_default_timeout(config.selector_timeout_ms)
            context.set_default_navigation_timeout(config.navigation_timeout_ms)
            page = context.new_page()
            try:
                yield context, page
            finally:
                context.close()
        finally:
            browser.close()
