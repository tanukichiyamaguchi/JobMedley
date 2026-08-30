"""ブラウザが付ける要求ヘッダの出所を、**名前だけ**で探す。押さない。送信しない。

実測42回目、``observe-resume`` がブラウザの要求ヘッダを見せた。こちらが付けて
いないものが4つある::

    x-csrf-token / x-customer-user-id / x-customer-user-email / x-experiment-data

``x-csrf-token`` が無ければ POST は弾かれ、ログイン画面へ転送される。実測41回目に
レジュメAPIが返した5万字のHTMLの正体はこれである。

**値の出所は分からない。** meta タグかもしれない。埋め込みの JSON かもしれない。
storage かもしれない。当てにいけるが、当てても確かめる手段が無い (原則3)。

だからページを開いて **名前だけ** を集める。``meta`` の name/property、
``localStorage`` と ``sessionStorage`` のキー、``window`` の直下の名前。

**値は1つも読まない。** 読む仕掛けがそもそも無い -- ページから取り出すのは名前の
配列だけで、:func:`recon.header_sources.find_sources` は名前しか受け取らない。
``x-customer-user-email`` は運用者のメールアドレスであり、``x-csrf-token`` は
それだけで POST を通せる鍵である (12.7/13.2)。
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.browser.dom import (
    login_form_visible,
    wait_for_interactive,
    wait_for_structure_to_settle,
)
from jobmedley_scout.browser.navigation import goto
from jobmedley_scout.config.placeholders import Coord, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.capture_send import install_gate
from jobmedley_scout.recon.gate import GateMode, SendGate
from jobmedley_scout.recon.header_sources import WANTED_HEADERS, SourceCandidates, find_sources
from jobmedley_scout.recon.open_structure import redact_url

#: このコマンドが埋めうる座標キー。**まだ座標が無いので空。**
#:
#: 出所が分かってから、どう座標にするかを決める。先に欄を作ると、埋めるために
#: 推測することになる (原則3)。
OBSERVE_HEADERS_KEYS: tuple[str, ...] = ()

#: ページから名前だけを集める JS。**値を返す枝が1つも無い。**
#:
#: ``getAttribute("content")`` も ``localStorage.getItem`` も呼んでいない。
#: 呼べる形で書くと、後から誰かが「ついでに値も」と足せてしまう (13.2)。
COLLECT_NAMES: Final[str] = """
() => {
  const names = [];
  for (const el of document.querySelectorAll('meta[name], meta[property]')) {
    const n = el.getAttribute('name') || el.getAttribute('property');
    if (n) names.push('meta[' + n + ']');
  }
  const store = (label, s) => {
    try {
      for (let i = 0; i < s.length; i++) names.push(label + '.' + s.key(i));
    } catch (e) {}
  };
  store('localStorage', localStorage);
  store('sessionStorage', sessionStorage);
  try {
    for (const k of Object.keys(window)) {
      if (k.length > 3) names.push('window.' + k);
    }
  } catch (e) {}
  return names;
}
"""


class HeaderStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    NO_SESSION = "no_session"
    SESSION_EXPIRED = "session_expired"
    NOTHING_READ = "nothing_read"
    FOUND = "found"


@dataclass(frozen=True)
class HeaderObservation:
    """The whole run, in the shape the report needs."""

    requested_url: str
    landed_url: str = ""
    session_present: bool = True
    session_expired: bool = False
    candidates: SourceCandidates | None = None
    read_failed: str = ""

    def reached(self) -> HeaderStage:
        """The single stage the run actually reached. **報告はこれだけを見る。**"""
        chain: tuple[tuple[HeaderStage, bool], ...] = (
            (HeaderStage.NO_SESSION, self.session_present),
            (HeaderStage.SESSION_EXPIRED, self.session_present and not self.session_expired),
            (HeaderStage.NOTHING_READ, self.candidates is not None and self.candidates.seen > 0),
        )
        stopped: HeaderStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                raise ValueError(
                    f"HeaderObservation の状態が時系列と矛盾しています: {stopped.value} で"
                    f"止まったのに {stage.value} を通過した証拠がある"
                    " (報告を嘘にしないため停止)。"
                )
        return stopped or HeaderStage.FOUND

    def render(self) -> str:
        lines = ["要求ヘッダの出所探し (**押していません。送信していません**)", ""]
        stage = self.reached()

        if stage is HeaderStage.NO_SESSION:
            lines.append("  保存セッションがありません。段階1からやり直してください。")
            return "\n".join(lines)
        if stage is HeaderStage.SESSION_EXPIRED:
            lines.append("  セッションが切れています (ログイン画面が出ました)。")
            return "\n".join(lines)
        if stage is HeaderStage.NOTHING_READ or self.candidates is None:
            lines.append("  **ページから名前を1つも読めませんでした。**")
            if self.read_failed:
                lines.append(f"  読めなかった理由: {self.read_failed}")
            lines.append(
                "  → 0件なのか、読む仕掛けが動いていないのかは、この報告では決まりません。"
            )
            return "\n".join(lines)

        lines.append(f"  見た名前: {self.candidates.seen} 個 (**値は1つも読んでいません**)")
        lines.append("")
        for header in WANTED_HEADERS:
            found = self.candidates.by_header.get(header, ())
            if found:
                lines.append(f"  {header}")
                lines.extend(f"    候補: {name}" for name in found)
            else:
                lines.append(f"  {header}: **出所の候補が見つかりませんでした**")
        if missing := self.candidates.unresolved():
            lines.append("")
            lines.append(f"  **出所が分からないヘッダ: {', '.join(missing)}**")
            lines.append("  → ページの名前には無いということです。JSが組み立てているか、")
            lines.append("    Cookie から作っているか、そもそも要らないかのどれかです。")
        lines.append("")
        lines.append("**どれを使うかは機械が決めません** (原則3)。上の候補から選んでください。")
        lines.append("**このコマンドは値を1文字も読んでいません** (12.7/13.2)。")
        return "\n".join(lines)


def observe_headers(
    config: BrowserConfig,
    credentials_dir: Path,
    candidate_list_url: Coord[str],
) -> HeaderObservation:
    """Open the page and collect **names only**. 押す操作は存在しない。"""
    requested_url = require(candidate_list_url, used_by="recon.observe_headers.observe_headers")

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return HeaderObservation(requested_url=requested_url, session_present=False)

    gate = SendGate(mode=GateMode.BLOCK_THIRD_PARTY)
    with browser_context(config, storage_state=session) as (_context, page):
        install_gate(page, gate)
        gate.arm()
        try:
            goto(page, requested_url, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            if login_form_visible(page, config.selector_timeout_ms):
                return HeaderObservation(
                    requested_url=requested_url,
                    session_expired=True,
                    landed_url=redact_url(page.url),
                )
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            names, failed = _collect(page)
            return HeaderObservation(
                requested_url=requested_url,
                landed_url=redact_url(page.url),
                candidates=find_sources(names),
                read_failed=failed,
            )
        finally:
            gate.disarm()


def _collect(page: Any) -> tuple[tuple[str, ...], str]:
    """Names from the page. Returns ``(名前, 読めなかった理由)``.

    **失敗しても落とさない。** ただし黙らない -- 「0件」と「読めなかった」を
    同じ結果にすると、次にどこを見ればよいか決められない (原則2)。
    """
    collected: object = None
    failed = ""
    try:
        collected = page.evaluate(COLLECT_NAMES)
    except Exception as exc:  # noqa: BLE001 -- 読めなかったことを事実として残す
        failed = type(exc).__name__
    if not isinstance(collected, list):
        return (), failed or "名前の配列が返りませんでした"
    names: list[str] = []
    for item in collected:
        with suppress(Exception):
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
    return tuple(names), failed


__all__ = [
    "OBSERVE_HEADERS_KEYS",
    "HeaderObservation",
    "HeaderStage",
    "observe_headers",
]
