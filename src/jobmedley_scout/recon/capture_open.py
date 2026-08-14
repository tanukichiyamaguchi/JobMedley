"""段階3の探索: **遮断を武装したまま**、カードのボタンを1つずつ押して正体を見る。

なぜこのコマンドが要るのか (経緯は :mod:`recon.open_structure` の冒頭)。

段階2の ``observe-list`` は ``nav.drawer_close_selectors`` を確定できずに終わった。
カード本体のクリックではドロワーが開かず (実測7回目)、開くのはカードの中のボタン
だが、そのボタンの片方は **スカウト送信そのもの** で、どちらがどちらかは構造からは
決まらない。**押してよいボタンを観測だけで選ぶことはできない。**

このコマンドは前提をひっくり返す。fail-closed の遮断 (:class:`recon.gate.SendGate`)
を **押す前に** 武装しておけば、どのボタンを押しても非GETは全て中断される。送信は
物理的に起こらない。そして中断された非GETは、そのまま段階3の成果物になる --
どのボタンが送信路かが推測ではなく観測で分かる。

**このコマンドが唯一破ってはいけない不変条件は「武装してから押す」である。**
順序が逆になれば取り消せない送信が起きる。だから武装は探索ループの外側で行い、
``finally`` で必ず解除する。ループの中で武装/解除を繰り返さない -- 繰り返せば
「解除中に押す」経路が生まれる。

ブラウザ依存部はここに閉じ込め、判断は :mod:`recon.open_structure` (純粋) に置く
(13.4)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.browser.dom import (
    DomTree,
    dom_tree,
    login_form_visible,
    wait_for_interactive,
    wait_for_structure_to_settle,
)
from jobmedley_scout.browser.navigation import goto
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN, Coord, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.capture_send import install_gate
from jobmedley_scout.recon.gate import SendGate
from jobmedley_scout.recon.list_structure import (
    row_group_candidates,
    stable_tokens,
    subtree_sizes,
    token_counts,
)
from jobmedley_scout.recon.observe_list import _dismiss_tour
from jobmedley_scout.recon.open_structure import (
    ActionCandidate,
    BlockedRequest,
    card_action_candidates,
    close_candidates_in,
    opened_region,
    rank_send_candidates,
    vanished_region,
)
from jobmedley_scout.recon.yaml_paste import yaml_scalar as _scalar

#: このコマンドが埋めうる座標キー。
CAPTURE_OPEN_KEYS: tuple[str, ...] = (
    "nav.drawer_close_selectors",
    "api.send.paid.url_pattern",
)

#: ランディング時に開いている常駐モーダルを閉じるための器 (実測7回目に観測)。
#: **閉じるだけの操作なので外向きの送信は起きない。**
LANDING_MODAL_CLOSERS: tuple[str, ...] = ("a.c-modal__closer",)


@dataclass(frozen=True)
class AttemptResult:
    """One button press, and everything it revealed."""

    selector: str
    nth: int
    looks_like_send: bool
    clicked: bool
    #: クリック後に増えた構造トークン (ドロワーが開いた証拠)。
    gained: tuple[str, ...] = ()
    #: クリック後に消えた構造トークン (``u-is-hidden`` が外れた等)。
    lost: tuple[str, ...] = ()
    #: この1押しの間に遮断された非GET。
    blocked: tuple[BlockedRequest, ...] = ()
    #: 開いた領域の中にあった閉じるボタンの候補。
    close_candidates: tuple[str, ...] = ()
    #: 閉じる操作を試して、開いた領域が実際に消えたか。None は試していない。
    close_verified: bool | None = None

    @property
    def opened(self) -> bool:
        return bool(self.gained)


@dataclass(frozen=True)
class OpenObservation:
    """The whole run, in the shape the report needs."""

    requested_url: str
    landed_url: str = ""
    session_present: bool = True
    session_expired: bool = False
    tree_read: bool = True
    rows_found: bool = False
    #: 武装が実際に効いていたか。**False なら値を1つも出さない** (後述)。
    gate_armed: bool = False
    attempts: tuple[AttemptResult, ...] = ()
    note: str = ""
    trees: dict[str, DomTree] = field(default_factory=dict)

    def confirmed_close_selectors(self) -> tuple[str, ...]:
        """Close controls whose effect was **observed**, best first.

        「開いた領域の中にあった」だけでは候補にとどまる。**押したら実際に領域が
        消えた** ものだけを値にする (原則3)。
        """
        found: list[str] = []
        for attempt in self.attempts:
            if attempt.close_verified and attempt.close_candidates:
                for selector in attempt.close_candidates:
                    if selector not in found:
                        found.append(selector)
        return tuple(found)

    def all_blocked(self) -> tuple[BlockedRequest, ...]:
        return tuple(entry for attempt in self.attempts for entry in attempt.blocked)

    def render(self) -> str:
        lines = ["段階3の探索結果 (送信遮断を武装した状態で観測)", ""]

        if not self.session_present:
            lines.append("  保存セッションがありません。シークレットを設定してください。")
            return "\n".join(lines)
        if self.session_expired:
            lines.append("  **セッションが効いていません。** 一覧URLにパスワード欄がありました。")
            lines.append(f"  到達URL: {self.landed_url or '(記録なし)'}")
            return "\n".join(lines)
        if not self.gate_armed:
            # **武装できなかったら何も押していない。** 押していないなら報告する
            # 観測は無い。ここを「値なし」で済ませると、押していないのに押した
            # 前提の報告になる。
            lines.append("  **遮断を武装できなかったため、ボタンを1つも押していません。**")
            lines.append(f"  {self.note or '理由は記録されていません。'}")
            return "\n".join(lines)
        if not self.tree_read:
            lines.append("  DOMの木を読めませんでした (要素が無かったのではありません)。")
            return "\n".join(lines)
        if not self.rows_found:
            lines.append("  候補者の行を特定できませんでした。")
            lines.append("  座標 nav.list_ready_selector を確認してください。")
            return "\n".join(lines)

        lines.append(f"  押した候補: {len(self.attempts)} 個")
        for attempt in self.attempts:
            mark = " (クラス名がスカウト送信を名乗る部品)" if attempt.looks_like_send else ""
            state = "押せました" if attempt.clicked else "押せませんでした"
            lines.append(f"    - {attempt.selector} の {attempt.nth} 番目: {state}{mark}")
            if attempt.gained:
                shown = ", ".join(attempt.gained[:6])
                lines.append(f"        増えた構造 ({len(attempt.gained)}種): {shown}")
            if attempt.blocked:
                lines.append(f"        遮断した非GET: {len(attempt.blocked)} 件")
                for entry in rank_send_candidates(attempt.blocked)[:3]:
                    tag = " **目印を含む = 送信路の可能性が高い**" if entry.carried_sentinel else ""
                    lines.append(f"          {entry.redacted()}{tag}")
            if attempt.close_verified is not None:
                verdict = (
                    "領域が消えました (確認済み)" if attempt.close_verified else "消えませんでした"
                )
                lines.append(f"        閉じる操作: {verdict}")
        lines.append("")
        lines.extend(self._coordinate_lines())
        lines.append("")
        lines.append("**このコマンドは送信を1件も行っていません。** 武装中の非GETは全て")
        lines.append("中断されています (fail-closed)。中断された通信は上に構造だけを印字し、")
        lines.append("URLの会員ID・クエリ値は伏せてあります (13.2)。原文は構造ダンプにあります。")
        return "\n".join(lines)

    def _coordinate_lines(self) -> list[str]:
        out = ["config/site_coordinates.yaml の該当行:", ""]

        confirmed = self.confirmed_close_selectors()
        if confirmed:
            out.append(f"  nav.drawer_close_selectors: {_scalar(list(confirmed))}")
            out.append("    # 押したら開いた領域が実際に消えたものだけを載せています。")
        else:
            out.append(f"  nav.drawer_close_selectors: {UNRESOLVED_TOKEN}")
            if any(a.opened for a in self.attempts):
                out.append("    # ドロワーは開きましたが、閉じる操作の効果を確認できませんでした。")
            else:
                out.append("    # どのボタンでもドロワーは開きませんでした。")
                out.append("    # 別画面へ遷移する作りの可能性があります (到達URLを確認)。")

        sends = rank_send_candidates(self.all_blocked())
        with_sentinel = [entry for entry in sends if entry.carried_sentinel]
        if with_sentinel:
            out.append(f"  api.send.paid.url_pattern: {_scalar(with_sentinel[0].url)}")
            out.append("    # 件名・本文に混ぜた目印を運んでいた非GET = 送信路。")
            out.append("    # **この通信は中断済みで、送信は行われていません。**")
        elif sends:
            out.append(f"  api.send.paid.url_pattern: {UNRESOLVED_TOKEN}")
            out.append("    # 目印を運ぶ非GETはありませんでした (送信画面まで到達していない)。")
            out.append("    # 遮断した非GETの一覧は上のとおりです。")
        else:
            out.append(f"  api.send.paid.url_pattern: {UNRESOLVED_TOKEN}")
            out.append("    # 非GETは1件も発生しませんでした。")
        return out


def _counts(page: Any) -> Mapping[str, int]:
    tree = dom_tree(page)
    return token_counts(tree) if tree is not None else {}


def _drain(gate: SendGate, sentinel: str) -> tuple[BlockedRequest, ...]:
    """Take what the gate recorded so far and reset it for the next press."""
    taken = tuple(
        BlockedRequest(
            method=entry.method,
            url=entry.url,
            carried_sentinel=bool(entry.body and sentinel in entry.body),
        )
        for entry in gate.recorded
    )
    gate.clear()
    return taken


def _close_landing_modals(page: Any, timeout_ms: int) -> None:
    """Close whatever modal greeted us. **閉じるだけなので送信は起きない。**"""
    for selector in LANDING_MODAL_CLOSERS:
        with suppress(Exception):
            locator = page.locator(selector)
            for index in range(min(locator.count(), 3)):
                element = locator.nth(index)
                if element.is_visible():
                    element.click(timeout=timeout_ms)
                    return


def explore_card_actions(
    page: Any,
    *,
    tree: DomTree,
    row_index: int,
    sentinel: str,
    gate: SendGate,
    config: BrowserConfig,
    list_url: str,
    max_attempts: int = 4,
) -> tuple[AttemptResult, ...]:
    """Press each control in one card, gate already armed. Returns what was seen.

    **この関数は武装を行わない。** 武装は呼び出し側がループの外で済ませてある
    という前提で書かれている -- ここで武装/解除を扱うと、解除中に押す経路が
    生まれる。前提が崩れていないことは呼び出し側が ``gate.is_armed`` で確かめる。
    """
    sizes = subtree_sizes(tree)
    candidates = card_action_candidates(tree, sizes, row_index)
    results: list[AttemptResult] = []

    for candidate in candidates[:max_attempts]:
        selector = candidate.selector()
        before = _counts(page)
        nth = _nth_within_page(tree, sizes, candidate, selector)
        clicked = False
        try:
            page.locator(selector).nth(nth).click(timeout=config.selector_timeout_ms)
            clicked = True
        except Exception:
            pass
        wait_for_structure_to_settle(page, config.selector_timeout_ms)
        after_tree = dom_tree(page)
        after = token_counts(after_tree) if after_tree is not None else {}
        gained = opened_region(before, after)
        lost = vanished_region(before, after)
        blocked = _drain(gate, sentinel)

        closes: tuple[str, ...] = ()
        verified: bool | None = None
        if gained and after_tree is not None:
            closes = close_candidates_in(after_tree, subtree_sizes(after_tree), gained)
            if closes:
                verified = _try_close(page, closes, gained, config)

        results.append(
            AttemptResult(
                selector=selector,
                nth=nth,
                looks_like_send=candidate.looks_like_send,
                clicked=clicked,
                gained=gained,
                lost=lost,
                blocked=blocked,
                close_candidates=closes,
                close_verified=verified,
            )
        )
        # 次の1押しを独立させる。**再遷移で画面を戻す** -- 前の押下が残した状態の
        # 上に重ねると、増えた構造がどちらの押下によるものか分からなくなる。
        goto(page, list_url, config)
        wait_for_interactive(page, config.selector_timeout_ms)
        _dismiss_tour(page, config.selector_timeout_ms)
        _close_landing_modals(page, config.selector_timeout_ms)
        wait_for_structure_to_settle(page, config.selector_timeout_ms)
        _drain(gate, sentinel)  # 再遷移中の非GETは観測対象ではない
    return tuple(results)


def _nth_within_page(
    tree: DomTree, sizes: Sequence[int], candidate: ActionCandidate, selector: str
) -> int:
    """Document-order index of ``candidate`` among page-wide matches of ``selector``.

    セレクタは同じクラス集合を持つ他のカードの部品にも一致する。**押すのは1枚目の
    カードの中の1つだけ** なので、文書順で何番目かを木から数える。
    """
    wanted = set(candidate.tokens)
    seen = 0
    for index in range(len(tree.nodes)):
        node = tree.nodes[index]
        if node.tag != candidate.tag:
            continue
        if wanted.issubset(set(stable_tokens(node.tag, node.class_names))):
            if index == candidate.index:
                return seen
            seen += 1
    return 0


def _try_close(
    page: Any, closes: Sequence[str], opened: Sequence[str], config: BrowserConfig
) -> bool:
    """Press the close control and check the opened region actually went away."""
    for selector in closes:
        with suppress(Exception):
            page.locator(selector).first.click(timeout=config.selector_timeout_ms)
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            after_tree = dom_tree(page)
            if after_tree is None:
                continue
            after = token_counts(after_tree)
            if all(after.get(token, 0) == 0 for token in opened):
                return True
    return False


def capture_open(
    config: BrowserConfig,
    credentials_dir: Path,
    candidate_list_url: Coord[str],
    *,
    run_id: str = "recon",
) -> tuple[OpenObservation, DomTree | None]:
    """Explore one card's controls with the send gate armed the whole time."""
    from jobmedley_scout.recon.sentinel import make_sentinel

    requested_url = require(candidate_list_url, used_by="recon.capture_open.capture_open")
    sentinel = make_sentinel(run_id)

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return OpenObservation(requested_url=requested_url, session_present=False), None

    gate = SendGate()
    with browser_context(config, storage_state=session) as (_context, page):
        # **何よりも先に武装する。** 遷移や描画で走る非GETごと止まるが、それでよい --
        # このコマンドの目的は観測であって、媒体側の状態を進めることではない。
        install_gate(page, gate)
        gate.arm()
        try:
            if not gate.is_armed:
                return (
                    OpenObservation(
                        requested_url=requested_url,
                        gate_armed=False,
                        note="武装の確認に失敗しました。",
                    ),
                    None,
                )

            goto(page, requested_url, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            if login_form_visible(page, config.selector_timeout_ms):
                return (
                    OpenObservation(
                        requested_url=requested_url,
                        gate_armed=True,
                        session_expired=True,
                        landed_url=page.url,
                    ),
                    None,
                )

            _dismiss_tour(page, config.selector_timeout_ms)
            _close_landing_modals(page, config.selector_timeout_ms)
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            _drain(gate, sentinel)  # ここまでの非GETは観測対象ではない

            tree = dom_tree(page)
            if tree is None or tree.truncated:
                return (
                    OpenObservation(
                        requested_url=requested_url,
                        gate_armed=True,
                        tree_read=False,
                        landed_url=page.url,
                    ),
                    tree,
                )

            sizes = subtree_sizes(tree)
            rows = row_group_candidates(tree, sizes, [])
            if not rows:
                return (
                    OpenObservation(
                        requested_url=requested_url,
                        gate_armed=True,
                        landed_url=page.url,
                        rows_found=False,
                    ),
                    tree,
                )

            attempts = explore_card_actions(
                page,
                tree=tree,
                row_index=rows[0].members[0],
                sentinel=sentinel,
                gate=gate,
                config=config,
                list_url=requested_url,
            )
            observed = OpenObservation(
                requested_url=requested_url,
                landed_url=page.url,
                gate_armed=True,
                rows_found=True,
                attempts=attempts,
            )
            return observed, dom_tree(page)
        finally:
            # **必ず解除する。** 解除し忘れると、同じコンテキストを使う後続処理が
            # 静かに全て中断される。
            gate.disarm()
