"""Stage-3 reconnaissance: walk the **known** path to the send request.

:mod:`recon.capture_open` は導線が分からないときの手段である。押せるものを
安全な順に押し、開いたものを辿る。16回走らせて、フォームまでは届くようになった
が、送信路は取れないままだった -- 目隠しの探索は「次に押すべき1つ」を知らない
ので、必須欄を埋めずに送信を押し、弾かれる。

運用者が実画面を示してくれたので、**通る道はもう探さなくてよい**::

    一覧の「スカウトを送る」
      → 送信フォーム (左に経歴、右に入力欄)
        → **必須** スカウト対象求人 (押すと候補が出る。1件選ぶ)
        → メッセージテンプレート (任意。選ぶと本文が埋まる)
        → **必須** 本文
      → 「確認してスカウトを送る」
        → 確認の段
          → 「この内容でスカウトを送る」  ← ここで初めて送信が発火する

このコマンドはその順に辿り、**最後の押下の瞬間だけ** 送信路を観測する。
遮断は最初の押下より前から武装しているので、送信は物理的に起こらない。

**テンプレートは選ばない。** 選ぶと本文が自動で埋まり、こちらが書いた目印を
上書きする。目印が本文に載っていることだけが、遮断した非GETの中から送信路を
観測で選び出す根拠である (:mod:`recon.sentinel`)。テンプレートは必須ではない
ので、選ばずに済ませる。

**どの段で止まってもそう報告する。** 段は時系列の1本の鎖で、報告は
:meth:`SendWalk.reached` の1つの値だけを見る (実測2回目の嘘の再発防止)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
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
from jobmedley_scout.browser.navigation import goto, marker_present
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN, Coord, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.capture_open import (
    _drain,
    _measure_after_press,
    _nth_within_page,
    _write_one,
)
from jobmedley_scout.recon.capture_send import install_gate
from jobmedley_scout.recon.form_structure import (
    body_fields_in,
    confirm_root,
    disabled_submits_in,
    form_root,
    query_fields_in,
    submit_candidates_in,
    suggestion_items_in,
)
from jobmedley_scout.recon.gate import GateMode, SendGate
from jobmedley_scout.recon.list_structure import (
    indices_with_token,
    subtree_sizes,
    token_counts,
)
from jobmedley_scout.recon.observe_list import _dismiss_tour
from jobmedley_scout.recon.open_structure import (
    ActionCandidate,
    BlockedRequest,
    card_action_candidates,
    click_failure_kind,
    newly_present,
    opened_region,
    rank_send_candidates,
    redact_url,
    region_roots,
    validation_errors_in,
    vanished_region,
)
from jobmedley_scout.recon.payload_shape import (
    PayloadShape,
    idempotency_candidates,
    shape_of,
)
from jobmedley_scout.recon.yaml_paste import yaml_scalar as _scalar

#: このコマンドが埋めうる座標キー。
FOLLOW_SEND_KEYS: tuple[str, ...] = (
    "api.send.paid.url_pattern",
    "api.send.paid.payload_template",
)

#: フォームへ届くまでに許す押下の回数。
#:
#: 運用者の画面では一覧の「スカウトを送る」から **1押しで** フォームが開く。
#: ただし実測16回目には、カードのボタンがまずプロフィールのモーダルを開き、
#: その中の「スカウトを送る」でフォームが開く経路も観測されている。
#: **2段まで許して、それ以上は探索に化けるので許さない。**
MAX_OPENS = 3

#: 本文を書いたあと、送信に向かって前進を試みる回数。
#:
#: 運用者の画面では「確認してスカウトを送る」→ 確認の段 →「この内容でスカウトを
#: 送る」の2回である。**それ以上は探索に化けるので許さない。**
MAX_FORWARD = 2


class SendWalkStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**

    報告はこの1つの値だけを見る。独立したブール値を手で並べた順に検査すると、
    順序を1つ間違えるだけで「まだ起きていない工程の失敗」を理由に出してしまう
    (実測2回目の嘘)。順序は :meth:`SendWalk.reached` の1箇所に閉じ込める。
    """

    NO_SESSION = "no_session"
    SESSION_EXPIRED = "session_expired"
    NOT_RENDERED = "not_rendered"
    NO_ROWS = "no_rows"
    ARM_FAILED = "arm_failed"
    FORM_NOT_OPENED = "form_not_opened"
    NO_SUGGESTIONS = "no_suggestions"
    OFFER_NOT_CHOSEN = "offer_not_chosen"
    BODY_NOT_WRITTEN = "body_not_written"
    SUBMIT_NOT_PRESSED = "submit_not_pressed"
    SEND_NOT_OBSERVED = "send_not_observed"
    SEND_OBSERVED = "send_observed"


@dataclass(frozen=True)
class Step:
    """One rung of the walk, and what it actually did.

    ``detail`` は **定型句だけ** を持つ。Playwright の例外メッセージには要素の
    outerHTML (= 画面の文言) が混ざるので、そのまま外へ出さない (13.2)。
    """

    name: str
    done: bool
    detail: str = ""
    selector: str = ""
    gained: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()
    blocked: tuple[BlockedRequest, ...] = ()


@dataclass(frozen=True)
class FormHandle:
    """The form we reached, and where it sits in the tree."""

    tree: DomTree
    sizes: tuple[int, ...]
    root: int
    #: フォームを開いた押下で **前に1つも無かった** トークン。以降の探索範囲。
    region: tuple[str, ...]


@dataclass(frozen=True)
class SendWalk:
    """The whole walk, in the shape the report needs."""

    requested_url: str
    landed_url: str = ""
    session_present: bool = True
    session_expired: bool = False
    list_rendered: bool = False
    rows_found: bool = False
    gate_armed: bool = False
    form_opened: bool = False
    suggestions_seen: bool = False
    offer_chosen: bool = False
    body_written: bool = False
    submit_pressed: bool = False
    steps: tuple[Step, ...] = ()
    reads_allowed: tuple[str, ...] = ()
    #: 送信路の payload の **形** (値は含まない)。掴めなければ None。
    payload: PayloadShape | None = None
    note: str = ""

    def all_blocked(self) -> tuple[BlockedRequest, ...]:
        found: list[BlockedRequest] = []
        for step in self.steps:
            found.extend(step.blocked)
        return tuple(found)

    def carrier(self) -> BlockedRequest | None:
        """The blocked request that carried our sentinel = **the send path**."""
        for entry in self.all_blocked():
            if entry.carried_sentinel:
                return entry
        return None

    def reached(self) -> SendWalkStage:
        """The single stage the walk actually reached. **報告はこれだけを見る。**

        工程を時系列で1本の鎖にし、「最初に False になる工程」ちょうど1つを
        到達点とする。後の工程は前の工程を通らないと始まらないので、これは
        一意に決まる。

        **鎖の単調性を破る状態は嘘である。** 後の工程の証拠が True なのに前の
        工程が False なら、実行のどこかがブール値を事実と違えて立てている。
        その状態で報告を出すと必ず嘘になるので、報告せず例外にする。
        """
        chain: tuple[tuple[SendWalkStage, bool], ...] = (
            (SendWalkStage.NO_SESSION, self.session_present),
            (SendWalkStage.SESSION_EXPIRED, self.session_present and not self.session_expired),
            (SendWalkStage.NOT_RENDERED, self.list_rendered),
            (SendWalkStage.NO_ROWS, self.rows_found),
            (SendWalkStage.ARM_FAILED, self.gate_armed),
            (SendWalkStage.FORM_NOT_OPENED, self.form_opened),
            (SendWalkStage.NO_SUGGESTIONS, self.suggestions_seen),
            (SendWalkStage.OFFER_NOT_CHOSEN, self.offer_chosen),
            (SendWalkStage.BODY_NOT_WRITTEN, self.body_written),
            (SendWalkStage.SUBMIT_NOT_PRESSED, self.submit_pressed),
            (SendWalkStage.SEND_NOT_OBSERVED, self.carrier() is not None),
        )
        stopped: SendWalkStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                raise ValueError(
                    f"SendWalk の状態が時系列と矛盾しています: {stopped.value} で"
                    f"止まったのに {stage.value} を通過した証拠がある。"
                    " どこかがブール値を事実と違えて立てています (報告を嘘にしないため停止)。"
                )
        return stopped or SendWalkStage.SEND_OBSERVED

    def render(self) -> str:
        lines = ["段階3: 教わった導線をそのまま辿った結果", ""]
        stage = self.reached()

        if stage is SendWalkStage.NO_SESSION:
            lines.append("  保存セッションがありません。段階1からやり直してください。")
            return "\n".join(lines)
        if stage is SendWalkStage.SESSION_EXPIRED:
            lines.append("  セッションが切れています (ログイン画面が出ました)。")
            lines.append("  Copy as cURL からシークレットを取り直してください。")
            return "\n".join(lines)

        for step in self.steps:
            mark = "○" if step.done else "×"
            lines.append(f"  {mark} {step.name}")
            if step.selector:
                lines.append(f"      指した部品: {step.selector}")
            if step.detail:
                lines.append(f"      {step.detail}")
            if step.gained:
                lines.append(
                    f"      増えた構造 ({len(step.gained)}種): {', '.join(step.gained[:12])}"
                )
            if step.lost:
                lines.append(f"      消えた構造 ({len(step.lost)}種): {', '.join(step.lost[:12])}")
            if rejected := validation_errors_in(step.gained):
                lines.append("      **フォームが入力の不備を訴えました。**")
                lines.append(f"      不備の目印: {', '.join(rejected[:6])}")
            if step.blocked:
                lines.append(f"      遮断した非GET: {len(step.blocked)} 件")
                for entry in rank_send_candidates(step.blocked)[:3]:
                    tag = " **目印を運んでいる = 送信路**" if entry.carried_sentinel else ""
                    lines.append(f"        {entry.redacted()}{tag}")

        lines.append("")
        lines.append("config/site_coordinates.yaml の該当行:")
        lines.append("")
        carrier = self.carrier()
        if carrier is not None:
            # **会員IDは伏せたまま値にする。** 生のURLには会員番号が入る (13.2:
            # 偵察の出力に個人を指す値を残さない)。そして座標名が url_pattern で
            # ある以上、欲しいのは1件ぶんのURLではなく **形** である -- 伏せ字に
            # した時点でそれは形になっている。
            lines.append(f"  api.send.paid.url_pattern: {_scalar(redact_url(carrier.url))}")
            lines.append("    # 本文へ書き込んだ目印を、この非GETが載せて運んでいました。")
            lines.append("    # **この通信は中断済みで、送信は行われていません。**")
        else:
            lines.append(f"  api.send.paid.url_pattern: {UNRESOLVED_TOKEN}")
            for line in _why_unresolved(stage, self):
                lines.append(f"    # {line}")

        # **段階3の成果物はURLだけではない。** 形が無ければ、段階4は何を詰めて
        # 送ればよいのか分からない。
        if self.payload is not None:
            lines.append("  api.send.paid.payload_template: |")
            for line in self.payload.template.splitlines():
                lines.append(f"    {line}")
            lines.append("    # 値はすべて種別に伏せてあります (13.2)。")
            if self.payload.body_key:
                lines.append(f"    # 本文の入り口: {self.payload.body_key}")
        elif carrier is not None:
            # **掴んだのに読めなかった** のは、掴めなかったのとは違う事実である。
            lines.append(f"  api.send.paid.payload_template: {UNRESOLVED_TOKEN}")
            lines.append("    # 送信路は掴みましたが、本文をJSONとして読めませんでした。")
        else:
            lines.append(f"  api.send.paid.payload_template: {UNRESOLVED_TOKEN}")
            lines.append("    # 送信路そのものが観測できていないので、形も分かりません。")

        lines.append("")
        lines.append("**このコマンドは送信を1件も行っていません。**")
        if self.reads_allowed:
            lines.append(
                f"武装中に **通した読み取り** (GraphQL の query): {len(self.reads_allowed)} 件"
            )
            for url in self.reads_allowed[:5]:
                lines.append(f"  通した: {url}")
        lines.append("書き込み (mutation・その他の非GET) は全て止めて記録しました。")
        if self.payload is not None:
            lines.append("")
            lines.append(self.payload.render())
            if idem := idempotency_candidates(self.payload.header_names):
                lines.append(f"  冪等キーらしいヘッダ名: {', '.join(idem)}")
            else:
                # **無いことを「無い」と言い切らない** (原則3)。この1回に載って
                # いなかった、という以上のことは観測していない。
                lines.append(
                    "  冪等キーらしいヘッダ名: この1回の送信には載っていませんでした"
                    " (api.idempotency_header を null と決めるにはまだ足りません)"
                )
        if self.note:
            lines.append(self.note)
        return "\n".join(lines)


def _why_unresolved(stage: SendWalkStage, walk: SendWalk) -> tuple[str, ...]:
    """Why the coordinate stayed unresolved. **止まった段そのものを理由にする。**

    「たぶんこうだろう」を書かない (原則3)。書けるのは、鎖のどこで止まったかと、
    その段が何を待っていたかだけである。
    """
    if stage is SendWalkStage.FORM_NOT_OPENED:
        return (
            "送信フォーム (本文欄と押せるボタンを同時に含む領域) が現れませんでした。",
            "カードのボタンを押しても開かないなら、導線が変わった可能性があります。",
        )
    if stage is SendWalkStage.NO_SUGGESTIONS:
        return (
            "スカウト対象求人の欄を押しましたが、**候補が1件も現れませんでした**。",
            "候補が出ないと求人を選べず、必須欄が埋まらないので送信は発行されません。",
            "欄を押しただけでは候補が出ない作りなら、文字を打つ必要があります。",
        )
    if stage is SendWalkStage.OFFER_NOT_CHOSEN:
        return (
            "候補は現れましたが、**押しても欄に値が入りませんでした**。",
            "押した対象が候補の項目ではなく、それを包む器だった可能性があります。",
        )
    if stage is SendWalkStage.BODY_NOT_WRITTEN:
        return (
            "本文欄へ目印を書き込めませんでした (理由は上の段に出ています)。",
            "目印が無ければ、遮断した非GETのどれが送信路かを観測では決められません。",
        )
    if stage is SendWalkStage.SUBMIT_NOT_PRESSED:
        return (
            "必須欄は埋めましたが、**送信へ進むボタンを押せませんでした**。",
            "無効なままだったなら、埋めたつもりの欄がまだ足りていません。",
        )
    blocked = len(walk.all_blocked())
    return (
        f"送信ボタンまで押しましたが、遮断した {blocked} 件の非GETのどれにも",
        "目印が載っていませんでした。押下が送信を発火させていないか、送信が",
        "目印を本文に載せない形で運ばれています。推測で埋めません (原則3)。",
    )


@dataclass
class _Catch:
    """Carries the run's sentinel, and remembers the **shape** of the send request.

    ``_drain`` は遮断の記録を空にする。段階3のもう1つの成果物
    (``api.send.paid.payload_template``) は、その記録の本文からしか取れない。
    **消える前に読む場所** がここである。

    読むのは形だけで、本文そのものは持ち帰らない (13.2)。送信先の会員IDが
    載っているし、雛形に要るのは値ではなく形である。
    """

    sentinel: str
    shape: PayloadShape | None = None

    def drain(self, gate: SendGate) -> tuple[BlockedRequest, ...]:
        if self.shape is None:
            for entry in gate.recorded:
                if entry.body and self.sentinel in entry.body:
                    self.shape = shape_of(entry.body, entry.headers, self.sentinel)
                    break
        return _drain(gate, self.sentinel)


def _counts(page: Any) -> Mapping[str, int]:
    """Token counts for the page as it stands now.

    **``capture_open`` から借りない。** 借りると DOM の読み取りが向こうの名前空間
    を経由するので、こちらを差し替えても効かない -- 押す前の測定が空になり、
    「前に1つも無かったもの」が **画面全体** になる。それは実測6・9回目に踏んだ
    「開いた領域がページ全体になる」事故と同じ形である。
    """
    tree = dom_tree(page)
    return token_counts(tree) if tree is not None else {}


def _press(
    page: Any,
    tree: DomTree,
    sizes: Sequence[int],
    candidate: ActionCandidate,
    config: BrowserConfig,
) -> tuple[str, str]:
    """Press one candidate. Returns ``(selector, detail)``. **理由を握り潰さない。**

    通常のクリックが「見えて・動かず・有効で・イベントを受け取れる」の検査で
    満了したら、DOMのイベントを直接発火して押下を届ける。実測16回目、この画面の
    押下は **22回すべて** 通常のクリックが完了しなかった。逃げ道が無ければ、
    観測は一度も先へ進まない。
    """
    selector = candidate.selector()
    nth = _nth_within_page(tree, sizes, candidate, selector)
    target = page.locator(selector).nth(nth)
    try:
        target.click(timeout=config.selector_timeout_ms)
        return selector, ""
    except Exception as exc:  # noqa: BLE001 -- 分類だけを持ち回る (13.2)
        kind = click_failure_kind(str(exc))
    with suppress(Exception):
        target.dispatch_event("click", timeout=config.selector_timeout_ms)
        return selector, f"通常のクリックは完了せず ({kind})、DOMイベントで押下を届けました"
    return selector, f"押下が届きませんでした ({kind})"


def _after(
    page: Any, before: Mapping[str, int], config: BrowserConfig
) -> tuple[DomTree | None, Mapping[str, int], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Measure once the transient part has gone. Returns tree, counts, gained, lost, region."""
    tree, counts = _measure_after_press(page, before, config)
    return (
        tree,
        counts,
        opened_region(before, counts),
        vanished_region(before, counts),
        newly_present(before, counts),
    )


def _find_form(
    page: Any,
    tree: DomTree,
    sizes: Sequence[int],
    row_index: int,
    gate: SendGate,
    catch: _Catch,
    config: BrowserConfig,
) -> tuple[list[Step], FormHandle | None]:
    """Press towards the send form. **送信らしいボタンから押す。**

    導線が分かっているので、安全な順ではなく **目的の順** に押す。カードの
    「スカウトを送る」はクラス名が送信を名乗っている (実測: ``c-button`` +
    ``js-tour-guide-scout-button``)。
    """
    steps: list[Step] = []
    current_tree = tree
    current_sizes = tuple(sizes)
    region: tuple[str, ...] = ()
    pressed: set[str] = set()

    candidates = card_action_candidates(current_tree, current_sizes, row_index)
    # card_action_candidates は安全な順 (送信が最後)。ここでは逆にする --
    # 目的地は送信フォームであって、安全な寄り道ではない。
    queue = list(sorted(candidates, key=lambda c: (not c.looks_like_send, c.index)))

    for _ in range(MAX_OPENS):
        if not queue:
            break
        candidate = queue.pop(0)
        if (key := f"{candidate.tag}:{candidate.index}") in pressed:
            continue
        pressed.add(key)

        before = _counts(page)
        selector, detail = _press(page, current_tree, current_sizes, candidate, config)
        after_tree, _after_counts, gained, lost, fresh = _after(page, before, config)
        blocked = catch.drain(gate)

        if after_tree is not None:
            current_tree = after_tree
            current_sizes = subtree_sizes(after_tree)
        region = fresh

        root = form_root(current_tree, current_sizes, region) if region else None
        steps.append(
            Step(
                name="送信フォームを開く",
                done=root is not None,
                detail=detail
                or ("フォームが現れました" if root is not None else "まだ開いていません"),
                selector=selector,
                gained=gained,
                lost=lost,
                blocked=blocked,
            )
        )
        if root is not None:
            return steps, FormHandle(
                tree=current_tree, sizes=current_sizes, root=root, region=region
            )

        # **開いた領域の中の「スカウトを送る」だけを次に積む。**
        #
        # 実測16回目には、カードのボタンがまずプロフィールのモーダルを開き、その
        # 中の「スカウトを送る」でフォームが開く経路が観測された。1段だけ潜る。
        #
        # 送信を名乗るものに限るのは、ここが探索に化けるのを防ぐためである --
        # 領域の中の押せるものを片端から積むと、それは capture-open と同じ
        # 目隠しの探索であり、実測9〜13回目の事故がそのまま戻ってくる。
        if region:
            inner: list[ActionCandidate] = []
            for candidate_root in region_roots(current_tree, current_sizes, region):
                inner.extend(submit_candidates_in(current_tree, current_sizes, candidate_root))
            queue[:0] = [c for c in inner if c.looks_like_send]
    return steps, None


#: 候補を出させるために欄へ打ち込む文字。**空から順に、狭くない順に。**
#:
#: 実測18回目、欄を「押す」だけでは候補が1件も出なかった。押下は
#: ``dispatch_event`` で届けているが、**それは焦点を移さない**。入力補完の類は
#: ``focus`` か ``input`` で開くので、クリックのイベントだけでは何も起きない。
#:
#: 打つ内容は媒体の持ち物 (この事業所の求人) に依存するので、**当てにいかない**。
#: 広い順に試し、**どれで候補が出たかを報告する** -- 次の実行が推測ではなく
#: 観測から始められるようにするためである (原則3)。
#:
#: - ``""``   : 何も打たず焦点だけ。空の検索で全件返す作りならこれで出る
#: - ``" "``  : 欄の但し書きが「スペース区切りで検索」なので、空白は正当な入力
#: - ``"県"`` : 都道府県名47のうち43に含まれる。勤務地で引く欄に最も広く当たる
#:
#: **これは座標を推測で埋めることではない。** 埋める座標 (送信APIのURL) は
#: あくまで観測から得る。ここで選んでいるのは「観測を成立させるための操作」で
#: あって、報告する値ではない。
SUGGESTION_PROBES: tuple[str, ...] = ("", " ", "県")

#: 欄へ焦点を当てる一文。**押下では焦点は移らない。**
#:
#: ``el.focus()`` は可視性の検査を経ないので、覆われている欄にも届く。返すのは
#: 実際に焦点が乗ったかどうかで、**乗ったと言えるのは乗ったときだけ** である。
_FOCUS_JS = """
(el) => { el.focus(); return document.activeElement === el; }
"""

#: 欄へ文字を打ち込む一文。
#:
#: 値を素の代入で入れると、値を自前で追跡する作り (React 等) が変更に気付かない。
#: プロトタイプ側の setter を明示的に呼び、``input`` を冒泡させる。入力補完には
#: キー入力を見るものもあるので ``keydown``/``keyup`` も添える。
#:
#: 安全性: 打ち込みは押下ではない。外向きの通信は検索の読み取り (GraphQL の
#: query) だけで、それ以外の非GETは遮断が武装したまま止める。
_TYPE_JS = """
(el, text) => {
  const proto =
    el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
    : el instanceof HTMLInputElement ? HTMLInputElement.prototype
    : null;
  const setter = proto && Object.getOwnPropertyDescriptor(proto, 'value').set;
  el.focus();
  el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Process' }));
  if (setter) { setter.call(el, text); } else { el.value = text; }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Process' }));
  return el.value === text;
}
"""

#: 検索欄として受け入れる ``input`` の ``type``。
#:
#: クラス名ではなく **HTML の意味** で判定する。クラス名は媒体の都合で変わるが、
#: ``type`` は変わらない。空文字は ``type`` 未指定 = ``text`` である。
_QUERY_INPUT_TYPES: frozenset[str] = frozenset({"", "text", "search"})


def _input_type(page: Any, form: FormHandle, field_: ActionCandidate) -> str | None:
    """The field's ``type``. ``None`` if it could not be read. **文言ではない。**"""
    selector = field_.selector()
    nth = _nth_within_page(form.tree, form.sizes, field_, selector)
    try:
        value = page.locator(selector).nth(nth).evaluate("(el) => el.type || ''")
    except Exception:  # noqa: BLE001 -- 読めないだけ。判定できないものは使わない
        return None
    return str(value).lower()


def _touch(page: Any, form: FormHandle, field_: ActionCandidate, probe: str) -> tuple[bool, str]:
    """Focus the field and, if ``probe`` is non-empty, type it. Returns (ok, detail)."""
    selector = field_.selector()
    nth = _nth_within_page(form.tree, form.sizes, field_, selector)
    element = page.locator(selector).nth(nth)
    try:
        focused = bool(element.evaluate(_FOCUS_JS))
    except Exception:  # noqa: BLE001 -- 分類だけを持ち回る (13.2)
        return False, "欄へ焦点を当てられませんでした"
    if not probe:
        return focused, ("焦点を当てました" if focused else "焦点が乗りませんでした")
    try:
        typed = bool(element.evaluate(_TYPE_JS, probe))
    except Exception:  # noqa: BLE001
        return False, "欄へ打ち込めませんでした"
    return typed, ("打ち込みました" if typed else "打ち込んだ値が残りませんでした")


def _choose_offer(
    page: Any,
    form: FormHandle,
    gate: SendGate,
    catch: _Catch,
    config: BrowserConfig,
) -> tuple[list[Step], FormHandle, bool, bool]:
    """Open the job-offer typeahead and pick one. Returns steps, form, seen, chosen.

    **押すのではなく、焦点を当てて打ち込む。** 実測18回目、欄を押すだけでは候補が
    1件も出なかった -- 押下は ``dispatch_event`` で届けているが、それは焦点を
    移さないので、入力補完は開かない。
    """
    steps: list[Step] = []
    fields = query_fields_in(form.tree, form.sizes, form.root)
    if not fields:
        steps.append(
            Step(
                name="スカウト対象求人の欄を探す",
                done=False,
                detail="フォームの中に1行の入力欄がありません",
            )
        )
        return steps, form, False, False

    current = form
    for field_ in fields:
        kind = _input_type(page, current, field_)
        if kind not in _QUERY_INPUT_TYPES:
            # **試す価値の無いものを試さない。** ただし飛ばした事実は残す。
            steps.append(
                Step(
                    name="スカウト対象求人の欄を探す",
                    done=False,
                    detail=f"この欄は検索欄ではありません (type={kind or '読めません'})",
                    selector=field_.selector(),
                )
            )
            continue

        items: tuple[ActionCandidate, ...] = ()
        fresh: tuple[str, ...] = ()
        for probe in SUGGESTION_PROBES:
            before = _counts(page)
            ok, detail = _touch(page, current, field_, probe)
            after_tree, _counts_after, gained, lost, fresh = _after(page, before, config)
            blocked = catch.drain(gate)
            if after_tree is not None:
                sizes = subtree_sizes(after_tree)
                current = FormHandle(
                    tree=after_tree,
                    sizes=sizes,
                    root=form_root(after_tree, sizes, current.region) or current.root,
                    region=current.region,
                )
            items = suggestion_items_in(current.tree, current.sizes, fresh) if fresh else ()
            how = "焦点だけ" if not probe else f"打ち込んだ文字: {probe!r}"
            steps.append(
                Step(
                    name=f"スカウト対象求人の候補を出す ({how})",
                    done=bool(items),
                    detail=(
                        f"候補が {len(items)} 件現れました (タグ: {items[0].tag})"
                        if items
                        else (detail if ok else detail) + " / 候補は現れませんでした"
                    ),
                    selector=field_.selector(),
                    gained=gained,
                    lost=lost,
                    blocked=blocked,
                )
            )
            if items:
                break
        if not items:
            continue

        before = _counts(page)
        pick_selector, pick_detail = _press(page, current.tree, current.sizes, items[0], config)
        after_tree, _counts_after, gained, lost, _fresh = _after(page, before, config)
        blocked = catch.drain(gate)
        if after_tree is not None:
            sizes = subtree_sizes(after_tree)
            current = FormHandle(
                tree=after_tree,
                sizes=sizes,
                root=form_root(after_tree, sizes, current.region) or current.root,
                region=current.region,
            )
        held = _field_has_value(page, field_, current)
        steps.append(
            Step(
                name="候補を1件選ぶ",
                done=held,
                # **値そのものは出さない** (13.2)。入ったかどうかだけを述べる。
                detail=(
                    "欄に値が入りました"
                    if held
                    else (pick_detail or "押しましたが欄は空のままです")
                ),
                selector=pick_selector,
                gained=gained,
                lost=lost,
                blocked=blocked,
            )
        )
        return steps, current, True, held
    return steps, current, False, False


def _field_has_value(page: Any, field_: ActionCandidate, form: FormHandle) -> bool:
    """Whether the field now holds *something*. **中身は読まない、有無だけ。**"""
    selector = field_.selector()
    nth = _nth_within_page(form.tree, form.sizes, field_, selector)
    try:
        return bool(page.locator(selector).nth(nth).input_value().strip())
    except Exception:  # noqa: BLE001 -- 読めないなら「入った」とは言えない
        return False


def _write_body(
    page: Any, form: FormHandle, catch: _Catch, config: BrowserConfig
) -> tuple[Step, bool]:
    """Write the sentinel into the body. **求人を選んだ後に書く。**

    テンプレートは本文を自動で埋める作りなので、先に書くと上書きされる。
    このコマンドはテンプレートを選ばないが、求人の選択がテンプレートを既定で
    適用する可能性は残る -- だから **順序を守って最後に書く**。
    """
    from jobmedley_scout.recon.sentinel import sentinel_body

    fields = body_fields_in(form.tree, form.sizes, form.root)
    if not fields:
        return Step(
            name="本文へ目印を書き込む", done=False, detail="フォームの中に本文欄がありません"
        ), False

    text = sentinel_body(catch.sentinel)
    for field_ in fields:
        selector = field_.selector()
        nth = _nth_within_page(form.tree, form.sizes, field_, selector)
        outcome = _write_one(page.locator(selector).nth(nth), text, config)
        if outcome.wrote:
            how = " (通常の入力は通らず、DOMへ直接書きました)" if outcome.forced else ""
            return Step(
                name="本文へ目印を書き込む",
                done=True,
                detail=f"書き込みました{how}",
                selector=selector,
            ), True
    return (
        Step(
            name="本文へ目印を書き込む",
            done=False,
            detail=f"本文欄は {len(fields)} 個ありましたが、1つも書き込めませんでした",
        ),
        False,
    )


def _press_forward(
    page: Any,
    form: FormHandle,
    gate: SendGate,
    catch: _Catch,
    config: BrowserConfig,
    *,
    name: str,
) -> tuple[Step, FormHandle, tuple[str, ...]]:
    """Press the form's送信らしいボタン once. Returns the step, the new form, the fresh region."""
    candidates = submit_candidates_in(form.tree, form.sizes, form.root)
    if not candidates:
        blocked_by = disabled_submits_in(form.tree, form.sizes, form.root)
        detail = (
            f"押せるボタンがありません (無効なボタンが {len(blocked_by)} 個あります = "
            "必須欄がまだ足りていない)"
            if blocked_by
            else "押せるボタンがありません"
        )
        return Step(name=name, done=False, detail=detail), form, ()

    before = _counts(page)
    selector, detail = _press(page, form.tree, form.sizes, candidates[0], config)
    after_tree, _counts_after, gained, lost, fresh = _after(page, before, config)
    blocked = catch.drain(gate)
    current = form
    if after_tree is not None:
        sizes = subtree_sizes(after_tree)
        current = FormHandle(
            tree=after_tree,
            sizes=sizes,
            root=form_root(after_tree, sizes, form.region) or form.root,
            region=form.region,
        )
    return (
        Step(
            name=name,
            done=True,
            detail=detail or "押しました",
            selector=selector,
            gained=gained,
            lost=lost,
            blocked=blocked,
        ),
        current,
        fresh,
    )


def follow_send(
    config: BrowserConfig,
    credentials_dir: Path,
    candidate_list_url: Coord[str],
    list_ready_selector: Coord[str],
    *,
    run_id: str = "recon",
) -> SendWalk:
    """Walk the known path with the gate armed, and report where it stopped.

    武装の位置は :func:`recon.capture_open.capture_open` と同じ考え方である。
    一覧が描画されるまでは武装せず (一覧の読み込みも非GETなので、武装したまま
    では画面が出ない -- 実測1・5回目)、**最初の押下の直前に武装** して、
    最後まで武装したままにする。``finally`` で必ず解除する。
    """
    from jobmedley_scout.recon.sentinel import make_sentinel

    requested_url = require(candidate_list_url, used_by="recon.follow_send.follow_send")
    ready_selector = require(list_ready_selector, used_by="recon.follow_send.follow_send")
    row_token = ready_selector.split(",")[0].strip()
    sentinel = make_sentinel(run_id)

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return SendWalk(requested_url=requested_url, session_present=False)

    gate = SendGate(mode=GateMode.BLOCK_WRITES)
    with browser_context(config, storage_state=session) as (_context, page):
        install_gate(page, gate)
        try:
            goto(page, requested_url, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            if login_form_visible(page, config.selector_timeout_ms):
                return SendWalk(
                    requested_url=requested_url, session_expired=True, landed_url=page.url
                )

            _dismiss_tour(page, config.selector_timeout_ms)
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            if not marker_present(page, row_token, timeout_ms=config.selector_timeout_ms):
                return SendWalk(
                    requested_url=requested_url,
                    landed_url=page.url,
                    note=f"一覧の行 ({row_token}) が現れませんでした。",
                )

            tree = dom_tree(page)
            if tree is None:
                return SendWalk(
                    requested_url=requested_url, landed_url=page.url, list_rendered=True
                )
            rows = indices_with_token(tree, row_token)
            if not rows:
                return SendWalk(
                    requested_url=requested_url,
                    landed_url=page.url,
                    list_rendered=True,
                    note=f"行 {row_token} を読んだ木から取れませんでした。",
                )

            gate.arm()
            if not gate.is_armed:
                return SendWalk(
                    requested_url=requested_url,
                    landed_url=page.url,
                    list_rendered=True,
                    rows_found=True,
                    note="武装の確認に失敗しました。",
                )
            _drain(gate, sentinel)

            outcome = walk_form(
                page,
                tree=tree,
                row_index=rows[0],
                sentinel=sentinel,
                gate=gate,
                config=config,
            )
            return SendWalk(
                requested_url=requested_url,
                landed_url=page.url,
                list_rendered=True,
                rows_found=True,
                gate_armed=True,
                form_opened=outcome.form_opened,
                suggestions_seen=outcome.suggestions_seen,
                offer_chosen=outcome.offer_chosen,
                body_written=outcome.body_written,
                submit_pressed=outcome.submit_pressed,
                steps=outcome.steps,
                payload=outcome.payload,
                reads_allowed=tuple(redact_url(r.url) for r in gate.passed_reads),
            )
        finally:
            gate.disarm()


@dataclass(frozen=True)
class WalkOutcome:
    """What the walk did, with **no browser left in it**.

    ブラウザ依存部は :func:`follow_send` に閉じ込め、順序そのもの (求人を選ぶ →
    本文を書く → 前進する) はここで返す。13.4 の「テスト不能な部分を薄く保つ」
    -- 順序を間違えると1往復まるごと失うので、順序だけは手元で固定できる形にする。
    """

    steps: tuple[Step, ...] = ()
    form_opened: bool = False
    suggestions_seen: bool = False
    offer_chosen: bool = False
    body_written: bool = False
    submit_pressed: bool = False
    #: 目印を運んでいった非GETの **形** (値は含まない)。掴めなければ None。
    payload: PayloadShape | None = None


def walk_form(
    page: Any,
    *,
    tree: DomTree,
    row_index: int,
    sentinel: str,
    gate: SendGate,
    config: BrowserConfig,
) -> WalkOutcome:
    """Walk the taught order with the gate **already armed**.

    **この関数は武装しない。** 呼ぶ側が武装済みであることを前提にする -- 武装の
    位置は :func:`follow_send` の設計そのものなので、2箇所に散らさない。
    """
    sizes = subtree_sizes(tree)
    catch = _Catch(sentinel=sentinel)
    steps, form = _find_form(page, tree, sizes, row_index, gate, catch, config)
    if form is None:
        return WalkOutcome(steps=tuple(steps), payload=catch.shape)

    offer_steps, form, seen, chosen = _choose_offer(page, form, gate, catch, config)
    steps.extend(offer_steps)
    if not chosen:
        return WalkOutcome(
            steps=tuple(steps),
            form_opened=True,
            suggestions_seen=seen,
            payload=catch.shape,
        )

    body_step, wrote = _write_body(page, form, catch, config)
    steps.append(body_step)
    if not wrote:
        return WalkOutcome(
            steps=tuple(steps),
            form_opened=True,
            suggestions_seen=True,
            offer_chosen=True,
            payload=catch.shape,
        )

    # **確認の段は「段」であって「関門」ではない。**
    #
    # 「確認してスカウトを送る」→ 確認 →「この内容でスカウトを送る」が運用者の
    # 見た形だが、押したら即座に送る作りもありうる。確認の段を鎖の一段にすると、
    # 確認が無いまま送信を観測した実行で単調性が破れ、**成功したのに報告が例外で
    # 落ちる**。だから前進は繰り返しで表し、押した事実は段として残しつつ、鎖には
    # 載せない。
    submitted = False
    names = ("「確認してスカウトを送る」を押す", "確認の段の送信ボタンを押す")
    for round_ in range(MAX_FORWARD):
        step, form, fresh = _press_forward(
            page, form, gate, catch, config, name=names[min(round_, len(names) - 1)]
        )
        steps.append(step)
        if not step.done:
            break
        submitted = True
        if any(entry.carried_sentinel for entry in step.blocked):
            # 目印を運ぶ非GETを掴んだ。これ以上押す理由が無い。
            break
        root = confirm_root(form.tree, form.sizes, fresh) if fresh else None
        steps.append(
            Step(
                name="確認の段が現れたか",
                done=root is not None,
                detail=(
                    "確認の段が現れました"
                    if root is not None
                    else "確認の段は現れませんでした (ここで前進を止めます)"
                ),
            )
        )
        if root is None:
            break
        form = FormHandle(tree=form.tree, sizes=form.sizes, root=root, region=tuple(fresh))

    return WalkOutcome(
        steps=tuple(steps),
        form_opened=True,
        suggestions_seen=True,
        offer_chosen=True,
        body_written=True,
        submit_pressed=submitted,
        payload=catch.shape,
    )
