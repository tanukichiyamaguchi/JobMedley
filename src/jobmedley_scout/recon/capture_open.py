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
from enum import StrEnum
from pathlib import Path
from typing import Any

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.browser.dom import (
    DomTree,
    dom_tree,
    login_form_visible,
    wait_for_any_detached,
    wait_for_content_to_arrive,
    wait_for_interactive,
    wait_for_structure_to_settle,
)
from jobmedley_scout.browser.navigation import goto, marker_present
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN, Coord, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.capture_send import install_gate
from jobmedley_scout.recon.gate import GateMode, SendGate
from jobmedley_scout.recon.list_structure import (
    indices_with_token,
    stable_tokens,
    subtree_sizes,
    token_counts,
)
from jobmedley_scout.recon.observe_list import _dismiss_tour
from jobmedley_scout.recon.open_structure import (
    ActionCandidate,
    BlockedRequest,
    card_action_candidates,
    click_failure_kind,
    close_candidates_in,
    newly_present,
    opened_region,
    rank_send_candidates,
    redact_url,
    revealed_controls,
    revealed_text_fields,
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


class StopStage(StrEnum):
    """探索が実際に到達した地点。**時系列の順に並んでいる。**

    報告はこの1つの値だけを見る。独立したブール値を手で並べた順に検査すると、
    順序を1つ間違えるだけで「まだ起きていない工程の失敗」を理由に出してしまう
    (実測2回目の嘘)。順序は :meth:`OpenObservation.reached` の1箇所に閉じ込め、
    テストで固定する。
    """

    NO_SESSION = "no_session"
    SESSION_EXPIRED = "session_expired"
    NOT_RENDERED = "not_rendered"
    TREE_UNREAD = "tree_unread"
    NO_ROWS = "no_rows"
    ARM_FAILED = "arm_failed"
    EXPLORED = "explored"


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
    #: この押下がドロワーではなく **別画面への遷移** だったか。
    navigated: bool = False
    #: クリックが完了しなかった理由の分類 (open_structure.CLICK_FAILURE_KINDS)。
    #: 空文字は「完了した」。**生の例外メッセージは持たない** (13.2)。
    failure_kind: str = ""
    #: 通常のクリックが満了したので、DOMイベントを直接発火して押下を届けたか。
    dispatched: bool = False
    #: この押下の直前に、現れた領域の入力欄へ目印を書き込めたか。
    #: **False なら、この押下で遮断された非GETが送信路かどうかは判別できない。**
    sentinel_written: bool = False
    #: 押す直前に、現れた領域の中に在った書き込める部品の数。
    #: **0 と「在ったが書けなかった」は別の事実である。** 区別せずに
    #: 「書き込めていません」とだけ報告すると、次に何を直すか決められない。
    text_fields_seen: int = 0

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
    #: **既定は「未到達」= False。** どのブール値も「その工程を通過した証拠」を
    #: 意味し、未到達なら False であること -- そうでないと reached() の単調性が
    #: 既定値で破れる (tree_read の既定が True だと、描画前で止まった実行が
    #: 「木を読めた」ことになる)。実行側は通過した工程を明示的に立てる。
    tree_read: bool = False
    #: 一覧が実際に描画されたか (確定済みの nav.list_ready_selector で確認)。
    #: **False なら押す対象が存在しない画面である。** 実測1回目はここを確かめずに
    #: 進み、描画に失敗した画面のヘッダ (サイトのロゴ) を押した。
    list_rendered: bool = False
    rows_found: bool = False
    #: 武装が実際に効いていたか。**False なら値を1つも出さない** (後述)。
    gate_armed: bool = False
    attempts: tuple[AttemptResult, ...] = ()
    #: 武装中に **通した** 読み取り (URLは伏せ字済み)。通した事実も報告する --
    #: 緩和が黙って効いていると、運用者は何が守られているのか確かめられない。
    reads_allowed: tuple[str, ...] = ()
    note: str = ""
    trees: dict[str, DomTree] = field(default_factory=dict)

    def reached(self) -> StopStage:
        """The single stage the run actually reached. **報告はこれだけを見る。**

        これがこのモジュールの反嘘の要である。報告が独立したブール値を手で並べた
        順に検査すると、順序を1つ間違えるだけで、途中で止まった実行に **まだ
        起きていない工程の失敗** が理由として付く (実測2回目: 行を取れなかった
        実行に「武装できなかった」と書いた)。

        代わりに、工程を **時系列で1本の鎖** にする。各工程には「そこを通過した
        証拠となるブール値」がある。実際に到達した地点は「鎖の中で最初に False に
        なる工程」ちょうど1つに決まる -- 後の工程は前の工程を通らないと始まらない
        からである。報告はこの1つの値だけを見るので、順序を二度と間違えない。

        **鎖の単調性を破る状態は嘘である。** 後の工程の証拠が True なのに前の工程が
        False なら、実行のどこかがブール値を事実と違えて立てている。その状態で
        報告を出すと必ず嘘になるので、**報告せず例外にする** (握り潰さない)。
        """
        chain: tuple[tuple[StopStage, bool], ...] = (
            (StopStage.NO_SESSION, self.session_present),
            # 期限切れの検査は「セッションがある」ときにしか通過しえない。
            # ``not session_expired`` 単体だと、セッションが無い実行 (後の工程は
            # すべて未到達) でこの工程だけ「通過」に見え、単調性が既定値で破れる。
            (StopStage.SESSION_EXPIRED, self.session_present and not self.session_expired),
            (StopStage.NOT_RENDERED, self.list_rendered),
            (StopStage.TREE_UNREAD, self.tree_read),
            (StopStage.NO_ROWS, self.rows_found),
            (StopStage.ARM_FAILED, self.gate_armed),
        )
        stopped: StopStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                # 前の工程で止まったはずなのに、後の工程の証拠が立っている。
                raise ValueError(
                    f"OpenObservation の状態が時系列と矛盾しています: "
                    f"{stopped.value} で止まったのに {stage.value} を通過した証拠がある。"
                    " どこかがブール値を事実と違えて立てています (報告を嘘にしないため停止)。"
                )
        return stopped or StopStage.EXPLORED

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

        # **報告は「実際に到達した地点」1つだけを見る** (reached の docstring)。
        stage = self.reached()
        if stage is StopStage.NO_SESSION:
            lines.append("  保存セッションがありません。シークレットを設定してください。")
            return "\n".join(lines)
        if stage is StopStage.SESSION_EXPIRED:
            lines.append("  **セッションが効いていません。** 一覧URLにパスワード欄がありました。")
            lines.append(f"  到達URL: {self.landed_url or '(記録なし)'}")
            return "\n".join(lines)
        if stage is StopStage.NOT_RENDERED:
            # **描画されていない画面で押さない** (実測1回目の失敗)。武装したまま
            # 遷移すると一覧のデータ読み込みごと止まり、押す対象が存在しなくなる。
            lines.append("  **一覧が描画されなかったため、ボタンを1つも押していません。**")
            lines.append(f"  {self.note or '理由は記録されていません。'}")
            lines.append(f"  到達URL: {self.landed_url or '(記録なし)'}")
            return "\n".join(lines)
        if stage is StopStage.TREE_UNREAD:
            lines.append("  DOMの木を読めませんでした (要素が無かったのではありません)。")
            return "\n".join(lines)
        if stage is StopStage.NO_ROWS:
            lines.append("  **候補者の行を取れなかったため、ボタンを1つも押していません。**")
            lines.append(f"  {self.note or '理由は記録されていません。'}")
            return "\n".join(lines)
        if stage is StopStage.ARM_FAILED:
            # **武装できなかったら何も押していない。** 押していないなら報告する
            # 観測は無い。ここを「値なし」で済ませると、押していないのに押した
            # 前提の報告になる。
            lines.append("  **遮断を武装できなかったため、ボタンを1つも押していません。**")
            lines.append(f"  {self.note or '理由は記録されていません。'}")
            return "\n".join(lines)

        lines.append(f"  押した候補: {len(self.attempts)} 個")
        for attempt in self.attempts:
            mark = " (クラス名がスカウト送信を名乗る部品)" if attempt.looks_like_send else ""
            state = "押せました" if attempt.clicked else "クリックは完了しませんでした"
            lines.append(f"    - {attempt.selector} の {attempt.nth} 番目: {state}{mark}")
            if attempt.failure_kind:
                # **理由を必ず出す。** 実測4回目は8個すべて「完了しませんでした」
                # としか言えず、何が起きているのか運用者にも開発側にも分からなかった。
                detail = (
                    "押下をDOMイベントで直接届けました"
                    if attempt.dispatched
                    else "押下は届いていません"
                )
                lines.append(f"        理由: {attempt.failure_kind} / {detail}")
            if not attempt.clicked and (attempt.gained or attempt.lost):
                # **どちらの事実も消さない。** クリックが完了しなかったのに画面が
                # 変わったのは実際に起きる (押下は伝わり、その直後の安定性検査で
                # 満了する)。「押せなかった」だけだと変化を、「押せた」だけだと
                # 完了しなかった事実を、それぞれ握り潰すことになる。
                lines.append("        ただし画面の構造は変化しました (押下は伝わった可能性)。")
            if attempt.navigated:
                lines.append(f"        **別画面へ遷移しました** (到達URL: {self.landed_url})")
                lines.append("        ここで探索を打ち切りました (知らない画面を押し進めない)。")
            if attempt.gained:
                # **多めに出す。** クラス名は構造であってページの文言ではない
                # (13.2)。6件で切ると、実測6回目のように読み込み表示だけが並んで
                # 「その後ろに何が現れたのか」が報告から落ちる。
                shown = ", ".join(attempt.gained[:14])
                lines.append(f"        増えた構造 ({len(attempt.gained)}種): {shown}")
            if attempt.lost:
                # **消えたものも観測である。** 待機していた領域は
                # ``u-is-hidden`` が外れる形で開くので、増えるのではなく消える。
                # 出さないと「何も起きなかった」に見える。
                shown = ", ".join(attempt.lost[:14])
                lines.append(f"        消えた構造 ({len(attempt.lost)}種): {shown}")
            if attempt.sentinel_written:
                lines.append("        押す前に、現れた領域の入力欄へ目印を書き込みました。")
            elif attempt.text_fields_seen:
                # **「無かった」と「在ったが書けなかった」を区別する。**
                lines.append(
                    f"        現れた領域に書き込める部品が {attempt.text_fields_seen} 個"
                    " ありましたが、1つも書き込めませんでした。"
                )
            if attempt.blocked:
                lines.append(f"        遮断した非GET: {len(attempt.blocked)} 件")
                for entry in rank_send_candidates(attempt.blocked)[:3]:
                    carried = entry.carried_sentinel
                    tag = " **書き込んだ目印を運んでいる = 送信路**" if carried else ""
                    lines.append(f"          {entry.redacted()}{tag}")
            if attempt.close_verified is not None:
                verdict = (
                    "領域が消えました (確認済み)" if attempt.close_verified else "消えませんでした"
                )
                lines.append(f"        閉じる操作: {verdict}")
        lines.append("")
        lines.extend(self._coordinate_lines())
        lines.append("")
        lines.append("**このコマンドは送信を1件も行っていません。**")
        if self.reads_allowed:
            # **通したものを黙らない。** 緩和が効いた実行で「非GETは全て止めた」と
            # 書けば、それは事実と違う。何を通したかを数と実物で述べる。
            lines.append(
                f"武装中に **通した読み取り** (GraphQL の query): {len(self.reads_allowed)} 件"
            )
            for url in self.reads_allowed[:5]:
                lines.append(f"  通した: {url}")
            lines.append("書き込み (mutation・その他の非GET) は全て止めて記録しました。")
            lines.append("止めた通信は媒体のサーバへ到達していません。")
        else:
            lines.append("武装中の非GETは全て止めています (fail-closed)。")
        lines.append("止めた通信は上に構造だけを印字し、URLの会員ID・クエリ値は")
        lines.append("伏せてあります (13.2)。原文は構造ダンプにあります。")
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
                # **「ドロワーが開いた」と言わない。** 観測したのは「構造が増えた」
                # であって、それがドロワーかは分かっていない。実測3回目で増えたのは
                # 一括スカウト用の選択バーで、閉じるものですらなかった。
                out.append("    # 押した結果、新しい構造は現れましたが、それが")
                out.append("    # 閉じられる領域 (ドロワー/モーダル) かは確認できていません。")
                out.append("    # 現れた構造は上の一覧のとおりです。")
            elif any(a.navigated for a in self.attempts):
                out.append("    # ドロワーではなく **別画面へ遷移** する作りでした。")
                out.append("    # 詳細が別画面で開くなら、この座標は不要かもしれません。")
            else:
                out.append("    # どのボタンでもドロワーは開きませんでした。")

        sends = rank_send_candidates(self.all_blocked())
        with_sentinel = [entry for entry in sends if entry.carried_sentinel]
        wrote_sentinel = any(attempt.sentinel_written for attempt in self.attempts)
        if with_sentinel:
            out.append(f"  api.send.paid.url_pattern: {_scalar(with_sentinel[0].url)}")
            out.append("    # 押す直前に、現れた領域の入力欄へ書き込んだ目印を、")
            out.append("    # この非GETが本文に載せて運んでいました = 送信路。")
            out.append("    # **この通信は中断済みで、送信は行われていません。**")
        elif sends and wrote_sentinel:
            # **目印を書いたうえで運ばれなかった** のは意味のある観測である。
            # 送信は別の押下の先にある (画面遷移した等)。
            out.append(f"  api.send.paid.url_pattern: {UNRESOLVED_TOKEN}")
            out.append("    # 入力欄に目印を書き込んだうえで押しましたが、それを")
            out.append("    # 運ぶ非GETはありませんでした。遮断した非GETの候補:")
            for entry in sends[:3]:
                out.append(f"    #   {entry.redacted()}")
        elif sends:
            # **ここで「送信画面まで到達していない」と書いてはいけない。**
            # 目印を書き込めなかった実行では、到達したかどうか自体が分からない。
            # 分からないことを理由として述べれば、それは上に並んでいる遮断済み
            # 非GETの一覧と矛盾しうる -- 報告が嘘になる。
            out.append(f"  api.send.paid.url_pattern: {UNRESOLVED_TOKEN}")
            out.append("    # 非GETは遮断しましたが、**目印を1文字も書き込めていません**")
            out.append("    # (押す直前に現れた領域に、書き込める入力欄が無かった)。")
            out.append("    # 目印が無ければ、下の候補のどれが送信路かを **観測では**")
            out.append("    # 決められません。推測で埋めない (原則3) ため未確定のままにします。")
            for entry in sends[:3]:
                out.append(f"    #   候補: {entry.redacted()}")
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
    max_attempts: int = 14,
) -> tuple[AttemptResult, ...]:
    """Press each control in one card, gate already armed. Returns what was seen.

    **この関数は武装を行わない。** 武装は呼び出し側がループの外で済ませてある
    という前提で書かれている -- ここで武装/解除を扱うと、解除中に押す経路が
    生まれる。前提が崩れていないことは呼び出し側が ``gate.is_armed`` で確かめる。

    **押した後に再遷移しない** (実測1回目の失敗から)。再遷移には一覧データの
    読み込みが要るが、武装中は非GETが全て止まるので画面が戻らない。戻らない画面で
    次を押せば、押しているのは別物である。

    **現れたものを次に押す** (実測3回目で分かった形)。カードのチェックボックスを
    押すと一括スカウト用のバー (``div.c-sticky-scout-bar``) が現れ、その中に
    スカウトボタンがある。押した結果現れたものを辿らない限り送信画面には
    到達しない。だから、増えた領域の中のコントロールを待ち行列に足して続ける。

    以前はここで「閉じられなければ打ち切る」としていた。閉じられなかったのは
    バグではなく **閉じるものではなかったから** である (選択バーはモーダルでは
    ない)。閉じる試みは続けるが、閉じられないことを打ち切りの理由にはしない --
    それをすると導線の途中で必ず止まる。

    打ち切るのは **別画面へ遷移したとき** だけ。武装したまま知らない画面を
    押し進めない。
    """
    sizes = subtree_sizes(tree)
    # 待ち行列は (押す候補, その候補が現れた領域のトークン) の対。領域を覚えて
    # おくのは、**押す直前にその領域の入力欄へ目印を書き込む** ためである
    # (revealed_text_fields の docstring)。カードの中の候補は最初から在るので
    # 「現れた領域」を持たない。
    queue: list[tuple[ActionCandidate, tuple[str, ...]]] = [
        (candidate, ()) for candidate in card_action_candidates(tree, sizes, row_index)
    ]
    results: list[AttemptResult] = []
    url_before_all = page.url
    pressed: set[str] = set()
    # **同じ領域を二度は辿らない。**
    #
    # 実測10回目、探索はチェックボックスを入れたり外したりし続けた。入れると
    # ``--checked`` が現れ、外すと ``--scouted`` が現れる -- 交互に「新しい領域が
    # 現れた」ことになり、そのたびに同じ候補が積み直される。押す対象の重複は
    # ``pressed`` が防ぐが、**領域の重複は防いでいなかった。**
    followed: set[tuple[str, ...]] = set()
    current_tree = tree
    current_sizes = sizes

    while queue and len(results) < max_attempts:
        candidate, region = queue.pop(0)
        selector = candidate.selector()
        nth = _nth_within_page(current_tree, current_sizes, candidate, selector)
        if (fingerprint := f"{selector}#{nth}") in pressed:
            continue  # 同じものを二度押さない (現れた領域が重なると重複しうる)
        pressed.add(fingerprint)
        # **押す前に目印を書き込む。** これをしないと、遮断した非GETのどれが
        # 送信路かを区別する根拠が無く、座標を推測で埋めることになる (原則3)。
        # 書き込みは送信ではない。遮断は武装したままである。
        fields = revealed_text_fields(current_tree, current_sizes, region)
        sentinel_written = _write_sentinel(page, fields, sentinel, config)
        before = _counts(page)
        clicked = False
        failure_kind = ""
        dispatched = False
        target = page.locator(selector).nth(nth)
        try:
            target.click(timeout=config.selector_timeout_ms)
            clicked = True
        except Exception as exc:  # noqa: BLE001 -- 理由を分類して必ず残す
            # **理由を握り潰さない** (実測4回目: 8個すべて失敗したのに理由が
            # 記録されておらず、次の手が打てなかった)。分類だけを持ち回る --
            # 生のメッセージには要素の outerHTML が混ざる (13.2)。
            failure_kind = click_failure_kind(str(exc))
            # **押下を届ける最後の手段。** Playwright の click は「見えて・動かず・
            # 有効で・イベントを受け取れる」まで待つ。画面下部に固定表示される
            # 一括操作バーのような作りでは、この検査が満了し続けて **一度も押せない**。
            # dispatch_event は DOM のイベントを直接発火するので検査を経ない。
            #
            # 安全性: 押す対象の選定はこれで変わらない (カードの中、または直前に
            # 現れた領域の中に限定済み)。そして遮断は武装したままなので、送信は
            # 物理的に起こらない。**「押せない」で観測を諦めるより、届く経路を
            # 用意して事実を持ち帰る。**
            with suppress(Exception):
                target.dispatch_event("click", timeout=config.selector_timeout_ms)
                dispatched = True
        # **中身が届くまで待ってから測る。** 構造の静止だけを見ると、読み込み
        # 表示 (``div.c-loader``) が出ている間も「落ち着いた」になり、押して
        # 現れたものが読み込み表示そのものになる (実測6回目)。
        # **2回測る。** 1回目には読み込み表示が写り込む -- 構造の静止だけを見ても
        # 区別できない (読み込み中も要素数は変わらない)。実測6・7回目はどちらも
        # 「増えた構造」が div.c-loader 一色で、その後ろに現れたものが見えなかった。
        #
        # 1回目に現れたものの中から **どれか1つが消える** のを待ってから測り直す。
        # 消えるのは読み込み表示であり、残るのが開いた領域である。全部が消えるのを
        # 待つと必ず満了し、何も待たないと読み込み表示を測る -- その中間が要る。
        after_tree, after = _measure_after_press(page, before, config)
        gained = opened_region(before, after)
        lost = vanished_region(before, after)
        # **報告と、押しに行く先は別物である。**
        # gained は「数が増えたもの」= 何が変わったかの事実 (報告用)。
        # region は「前には1つも無かったもの」= その押下が開いた領域 (探索用)。
        # 混ぜると、画面の至る所に在るトークンが1つ増えただけでページ全体が
        # 「開いた領域」になる (実測9回目: ヘッダのログアウトリンクを押した)。
        region = newly_present(before, after)
        blocked = _drain(gate, sentinel)
        navigated = page.url != url_before_all

        closes: tuple[str, ...] = ()
        verified: bool | None = None
        if region and after_tree is not None and not navigated:
            closes = close_candidates_in(after_tree, subtree_sizes(after_tree), region)
            if closes:
                verified = _try_close(page, closes, region, config)

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
                navigated=navigated,
                failure_kind=failure_kind,
                dispatched=dispatched,
                sentinel_written=sentinel_written,
                text_fields_seen=len(fields),
            )
        )
        if navigated:
            # 別画面へ移ってしまった。武装したまま知らない画面を押し進めない。
            break
        if after_tree is not None:
            # **現れたものを次に押す。** これが送信路への導線である (docstring)。
            current_tree = after_tree
            current_sizes = subtree_sizes(after_tree)
            if region and verified is not True and region not in followed:
                followed.add(region)
                # 閉じられなかった = 閉じるものではなかった (選択バー等)。
                # 打ち切らずに、現れた領域の中を次の候補として積む。
                # **いま開いたものの中を先に押す (深さ優先)。**
                #
                # 後ろに足すと、先に開いた領域の残りを全部押してから次へ進む。
                # 実測8回目はそれで迷子になった: スカウトのサイドカバーが開いた
                # (150種の新出) のに、次に押したのは1つ前に開いたプロフィール
                # モーダルの部品で、3手のうちに別画面へ遷移して終わった。
                #
                # 導線は「開いたものの中へ入っていく」形をしている。前に足せば
                # その形に沿って進む -- 送信フォームは、開いた領域の奥にある。
                queue[:0] = [
                    (control, region)
                    for control in revealed_controls(current_tree, current_sizes, region)
                ]
    return tuple(results)


def _write_sentinel(
    page: Any,
    fields: Sequence[ActionCandidate],
    sentinel: str,
    config: BrowserConfig,
) -> bool:
    """Write the sentinel into the revealed region's text fields. **押下ではない。**

    書き込みは送信ではない。フォームに文字を入れても外向きの通信は起きないし、
    下書き保存のような通信が走ったとしても、それは非GETなので遮断されている。

    **なぜやるのか** は :func:`recon.open_structure.revealed_text_fields` の
    docstring にある -- 目印が本文に載っていることだけが、遮断した非GETの中から
    送信路を **観測で** 選び出す根拠になる。

    書けなくても続ける (チェックボックスや hidden に当たれば ``fill`` は失敗する)。
    失敗は探索の失敗ではないので握り潰してよい -- ここで落とす事実は「書けた/
    書けなかった」の一点で、それは返り値として必ず報告に届く。
    """
    from jobmedley_scout.recon.sentinel import sentinel_body

    if not fields:
        return False
    text = sentinel_body(sentinel)
    wrote = False
    for field_ in fields:
        selector = field_.selector()
        try:
            locator = page.locator(selector)
            total = min(locator.count(), 3)
        except Exception:  # noqa: BLE001 -- 数えられないだけ。探索は続ける
            continue
        for index in range(total):
            with suppress(Exception):
                element = locator.nth(index)
                if not element.is_visible():
                    continue
                element.fill(text, timeout=config.selector_timeout_ms)
                wrote = True
    return wrote


def _measure_after_press(
    page: Any, before: Mapping[str, int], config: BrowserConfig
) -> tuple[DomTree | None, Mapping[str, int]]:
    """Read the DOM after a press, **once the transient part has gone**.

    手順は3つ。

    1. 構造が落ち着くのを待って1回目を測る (ここには読み込み表示が写る)
    2. 1回目に **新しく現れたもの** のうち、どれか1つが消えるのを待つ --
       消えるのが読み込み表示、残るのが開いた領域である
    3. もう一度落ち着くのを待って測り直す

    2 を「全部が消える」にすると、開いた領域は残るので必ず満了する。
    2 を省くと読み込み表示を「押して現れたもの」として測る (実測6・7回目)。
    **どちらでもない条件が要る。**

    待てなくても失敗にしない。消えるものが無い押下は正常にありうるので、その
    ときは1回目の測定がそのまま答えになる。
    """
    wait_for_structure_to_settle(page, config.selector_timeout_ms)
    early_tree = dom_tree(page)
    early = token_counts(early_tree) if early_tree is not None else {}

    appeared = opened_region(before, early)
    if appeared:
        wait_for_any_detached(page, list(appeared), config.selector_timeout_ms)
        wait_for_structure_to_settle(page, config.selector_timeout_ms)
        settled_tree = dom_tree(page)
        if settled_tree is not None:
            return settled_tree, token_counts(settled_tree)
    return early_tree, early


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
            wait_for_content_to_arrive(page, config.selector_timeout_ms)
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
    list_ready_selector: Coord[str],
    *,
    run_id: str = "recon",
) -> tuple[OpenObservation, DomTree | None]:
    """Explore one card's controls, arming the gate once the list has rendered.

    **武装の位置がこのコマンドの設計そのものである。**

    最初の実装は「何よりも先に武装する」だった。安全側ではあるが **画面が
    描画されない** -- 一覧のデータ読み込みは非GETで、それも遮断されるからである。
    実測1回目はカードが1枚も出ず、「繰り返し構造」がヘッダの要素になり、
    サイトのロゴを押した。送信は起きなかったが、観測としては無意味だった。

    いまの順序:

    1. 遮断は仕掛けるが **武装しない** ままページを開き、一覧が描画されるのを
       ``nav.list_ready_selector`` (確定済みの座標) で確かめる
    2. 行を **その座標の行トークンで** 選ぶ。「最も繰り返している構造」ではない --
       描画に失敗した画面ではヘッダが勝つ
    3. **押す直前に武装し**、探索の間ずっと武装したままにする
    4. ``finally`` で必ず解除する

    1と2が無いと、押す対象が「カードの中のボタン」である保証が消える。
    """
    from jobmedley_scout.recon.sentinel import make_sentinel

    requested_url = require(candidate_list_url, used_by="recon.capture_open.capture_open")
    ready_selector = require(list_ready_selector, used_by="recon.capture_open.capture_open")
    row_token = ready_selector.split(",")[0].strip()
    sentinel = make_sentinel(run_id)

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return OpenObservation(requested_url=requested_url, session_present=False), None

    # **書き込みだけを止める。** 媒体は GraphQL の単一ページアプリで、画面を
    # 開くための読み取りも POST で来る。全ての非GETを止めると、モーダルは中身を
    # 得られず共通エラー処理で ``/customers/network_error/`` へ飛ばされ、探索は
    # そこで終わる (実測5回目)。緩和の範囲と根拠は :mod:`recon.graphql` に書いた。
    #
    # **送信は依然として物理的に起こらない。** スカウト送信は GraphQL の
    # ``mutation`` (状態変更) なので、通す条件 (``query`` のみ) に当たらない。
    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    with browser_context(config, storage_state=session) as (_context, page):
        # 遮断は仕掛けるが、**まだ武装しない**。武装したまま遷移すると一覧の
        # データ読み込み (非GET) まで止まり、押す対象が存在しない画面になる。
        install_gate(page, gate)
        try:
            goto(page, requested_url, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            if login_form_visible(page, config.selector_timeout_ms):
                return (
                    OpenObservation(
                        requested_url=requested_url,
                        session_expired=True,
                        landed_url=page.url,
                    ),
                    None,
                )

            _dismiss_tour(page, config.selector_timeout_ms)
            _close_landing_modals(page, config.selector_timeout_ms)
            wait_for_structure_to_settle(page, config.selector_timeout_ms)

            # **一覧が本当に描画されたことを、確定済みの座標で確かめる。**
            # ここを確かめずに進むと、描画に失敗した画面のヘッダを押す。
            if not marker_present(page, row_token, timeout_ms=config.selector_timeout_ms):
                return (
                    OpenObservation(
                        requested_url=requested_url,
                        landed_url=page.url,
                        list_rendered=False,
                        note=f"一覧の行 ({row_token}) が現れませんでした。",
                    ),
                    dom_tree(page),
                )

            tree = dom_tree(page)
            if tree is None or tree.truncated:
                return (
                    OpenObservation(
                        requested_url=requested_url,
                        tree_read=False,
                        list_rendered=True,
                        landed_url=page.url,
                    ),
                    tree,
                )

            # **行は座標の行トークンを持つ節点そのもの。** 繰り返し構造の解析
            # (row_group_candidates) を経由してはいけない -- あれは極大性の規則で
            # 「外側の繰り返しに含まれる群」を落とすので、カードが別の繰り返し
            # 構造の内側にあると消える (実測2回目で行が div.c-segment に化けた
            # のと同じ規則)。capture-open の実測2回目はこれで行を取り逃した。
            # 座標が確定しているのだから、推定を経由せず直接指す。
            rows = indices_with_token(tree, row_token)
            if not rows:
                return (
                    OpenObservation(
                        requested_url=requested_url,
                        landed_url=page.url,
                        tree_read=True,
                        list_rendered=True,
                        rows_found=False,
                        note=(
                            f"行 {row_token} は画面に在りましたが、"
                            "読んだDOMの木からは取れませんでした。"
                        ),
                    ),
                    tree,
                )

            # **ここで武装する。押す直前であり、探索の間ずっと武装したままにする。**
            gate.arm()
            if not gate.is_armed:
                return (
                    OpenObservation(
                        requested_url=requested_url,
                        landed_url=page.url,
                        tree_read=True,
                        list_rendered=True,
                        rows_found=True,
                        gate_armed=False,
                        note="武装の確認に失敗しました。",
                    ),
                    tree,
                )
            _drain(gate, sentinel)  # 武装前の記録は無い。念のため空にしておく。

            attempts = explore_card_actions(
                page,
                tree=tree,
                row_index=rows[0],
                sentinel=sentinel,
                gate=gate,
                config=config,
                list_url=requested_url,
            )
            observed = OpenObservation(
                requested_url=requested_url,
                landed_url=page.url,
                gate_armed=True,
                tree_read=True,
                list_rendered=True,
                rows_found=True,
                attempts=attempts,
                reads_allowed=tuple(redact_url(entry.url) for entry in gate.passed_reads),
            )
            return observed, dom_tree(page)
        finally:
            # **必ず解除する。** 解除し忘れると、同じコンテキストを使う後続処理が
            # 静かに全て中断される。
            gate.disarm()
