"""Network response capture.

10.4 の事故:

> 参照実装では、ブートストラップで開いたマイページに **自社パイプラインの候補者IDが
> 載っていた** ため、部分文字列の照合が全員にヒットし、**検知した返信のうち大半が
> 偽陽性** になりました。
>
> ブートストラップの完了後、本番の照合を始める直前に、捕捉済みの応答を **全部
> 捨ててください。**

そこで本モジュールは、バッファを **:meth:`ResponseBuffer.measurement_window` 経由で
しか読めない** 設計にしてある。この文脈マネージャは **入場時に必ずクリアする** ので、
「ブートストラップで拾った応答が測定に混ざる」という事故が構文的に再現しにくい。

生の ``entries`` を直接読む API は意図的に用意していない。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CapturedResponse:
    url: str
    status: int
    body: str


@dataclass
class MeasurementWindow:
    """A view over responses captured *since this window opened*."""

    _entries: list[CapturedResponse] = field(default_factory=list)

    def matching(self, url_fragment: str) -> tuple[CapturedResponse, ...]:
        return tuple(entry for entry in self._entries if url_fragment in entry.url)

    def all_entries(self) -> tuple[CapturedResponse, ...]:
        return tuple(self._entries)

    @property
    def count(self) -> int:
        return len(self._entries)


class ResponseBuffer:
    """Captures response bodies, readable only through a clearing window.

    ``char_cap`` は1件あたりの保持文字数上限 (設定 ``reply.response_capture_char_cap``)。
    レジュメを含む応答が丸ごとメモリとログに載るのを防ぐ (13.2)。
    """

    def __init__(self, char_cap: int) -> None:
        self._entries: list[CapturedResponse] = []
        self._char_cap = char_cap
        self._attached = False

    def attach(self, page: Any, url_filter: str | None = None) -> None:
        """Start capturing. Safe to call once per page."""
        if self._attached:
            return

        def _on_response(response: Any) -> None:
            try:
                url = response.url
                if url_filter is not None and url_filter not in url:
                    return
                body = response.text()
            except Exception:
                # 本文を読めない応答 (バイナリ・既にクローズ済み) は無視してよい。
                # ここで例外を上げると、無関係な画像1枚でスキャンが止まる。
                return
            self._entries.append(
                CapturedResponse(url=url, status=response.status, body=body[: self._char_cap])
            )

        page.on("response", _on_response)
        self._attached = True

    def discard_all(self) -> int:
        """Throw away everything captured so far. Returns how many were dropped."""
        dropped = len(self._entries)
        self._entries.clear()
        return dropped

    @contextmanager
    def measurement_window(self) -> Iterator[MeasurementWindow]:
        """Open a measurement window, **discarding everything captured before it.**

        10.4 の対策の実体。ブートストラップ (マイページを開いてアプリを起動する)
        で拾った応答には自社の候補者IDが載っており、これを捨てないと部分文字列
        照合が全員にヒットする。
        """
        self.discard_all()
        window = MeasurementWindow(self._entries)
        try:
            yield window
        finally:
            # 窓を閉じたら次の測定に持ち越さない。
            self._entries.clear()
