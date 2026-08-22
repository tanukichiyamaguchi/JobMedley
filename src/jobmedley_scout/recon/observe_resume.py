"""段階3の残り: **レジュメ取得APIを、送信を遮断したまま観測する。**

``api.resume.url_pattern`` は段階3で唯一まだ埋まっていない読み取り座標である。
埋まらなかった理由ははっきりしている -- **レジュメはカードを押さないと飛ばない。**

``observe-api`` は1つも押さない。押さないことがあのコマンドの安全性そのもの
だった (媒体のオリジンを丸ごと素通しにするため)。だから一覧を開いたときに飛ぶ
ものしか聴けず、レジュメはそこに入らない。

**このコマンドは押す。** そのかわり遮断を許可制にする
(:data:`~recon.gate.GateMode.BLOCK_SEND`)::

    GraphQL          読み取り (query) だけ通す。mutation は止める
    REST の POST     観測済みの読み取り経路だけ通す (search 系 / label)
    それ以外         止める

**送信は mutation である** (実測: ``POST /api/customers/graphql/SendSingleScout``)。
だから押し間違えても、送信リクエストはサーバへ届かない (13.6)。

**一覧は REST の POST で来る** ので ``BLOCK_WRITES`` では止まってしまい、行が
消えてカードを押せない (実測22回目)。許可制はその両方を同時に満たす。

何を探しているか。実測5回目に遮断が記録していた::

    POST /api/customers/graphql/MemberOnScoutProfileModalOfDesktop

名前と、出る場面 (カードの「プロフィール確認」を押したとき) からはこれらしい。
**ただし応答の中身を見ていない** ので、レジュメの項目がここで取れるのかは
分からない。このコマンドはそれを、値を出さずに決着させる (原則3)。

**値は1文字も出さない** (13.2)。レジュメには居住地・年齢・学歴・資格・希望条件が
入っている。出すのはキーの名前と値の種別だけである。
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.browser.dom import (
    dom_tree,
    login_form_visible,
    wait_for_content_to_arrive,
    wait_for_interactive,
    wait_for_structure_to_settle,
)
from jobmedley_scout.browser.navigation import goto, marker_present
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN, Coord, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.api_shape import ObservedCall
from jobmedley_scout.recon.capture_send import install_gate
from jobmedley_scout.recon.gate import KNOWN_WRITE_MARK_READ, GateMode, SendGate
from jobmedley_scout.recon.list_structure import indices_with_token, subtree_sizes
from jobmedley_scout.recon.listen import ResponseShapeListener
from jobmedley_scout.recon.open_structure import (
    ActionCandidate,
    card_action_candidates,
    is_closing,
    is_disabled,
    is_forbidden,
    redact_url,
)
from jobmedley_scout.recon.resume_keys import KeyPath

#: このコマンドが埋めうる座標キー。
OBSERVE_RESUME_KEYS: tuple[str, ...] = ("api.resume.url_pattern",)

#: 押す上限。**1枚のカードで足りる。**
#:
#: レジュメは候補者ごとに同じ形なので、たくさん開いても分かることは増えない。
#: 増えるのは「意図しない何かを押す機会」だけである (13.6)。
MAX_PRESSES = 4

#: レジュメらしさを示すキー名の断片。**名前で探す。値は見ない。**
#:
#: 実画面で確認した項目 (2026-08-22 運用者提供)::
#:
#:     基本プロフィール  会員番号 / 氏名(ふりがな) / 性別 / 年齢 / 電話番号 /
#:                       居住地 / 現在月収 / 最終学歴 / 就業状況 / 資格 / 経験職種
#:     希望条件          希望職種 / 希望勤務地 / 希望勤務形態 / 希望入職時期 /
#:                       希望年収 / こだわり条件 / 気になる
#:     パーソナリティ    自己PR
#:     スカウト履歴      求人名 / 送信日 / 全N回
#:
#: **これは画面の項目名であって、APIのキーパスではない。** 当たりを付けるために
#: だけ使い、座標に書いてよいかは応答の形を見てから決める (原則3)。
RESUME_KEY_HINTS: tuple[str, ...] = (
    "selfpr",
    "self_pr",
    "career",
    "education",
    "qualification",
    "desired",
    "salary",
    "income",
    "employmentstatus",
    "employment_status",
    "scouted",
    "profile",
    "resume",
)


class ResumeStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    NO_SESSION = "no_session"
    SESSION_EXPIRED = "session_expired"
    NO_ROWS = "no_rows"
    NOTHING_PRESSED = "nothing_pressed"
    NOTHING_NEW_HEARD = "nothing_new_heard"
    HEARD = "heard"


@dataclass(frozen=True)
class PressAttempt:
    """One control we pressed, and what it was. **本文も文言も持たない。**"""

    selector: str
    #: 押せたか。押せなかったこと自体が観測なので残す。
    pressed: bool
    #: 押した後に新しく届いた応答の数。
    new_responses: int = 0
    #: 押せなかった理由 (定型句のみ)。
    failure: str = ""


@dataclass(frozen=True)
class ResumeObservation:
    """The whole run, in the shape the report needs."""

    requested_url: str
    session_present: bool = True
    session_expired: bool = False
    list_rendered: bool = False
    #: 一覧を開いた時点で届いていた応答。**レジュメではない。**
    before: tuple[ObservedCall, ...] = ()
    #: 押した後に **新しく** 届いた応答。ここにレジュメが居る。
    after: tuple[ObservedCall, ...] = ()
    attempts: tuple[PressAttempt, ...] = ()
    #: 遮断が **止めた** 通信。送信が飛べなかったことの証拠でもある。
    blocked: tuple[str, ...] = ()
    listener_attached: bool = False
    #: 2回目 (書き込みを1つ受け入れた試行) を行ったか。
    #:
    #: **行ったなら必ず報告に出す。** 何を書いたか分からないまま偵察が終わるのが
    #: 一番悪い。
    accepted_a_write: bool = False
    #: 実際に通した書き込み (URLは伏せ済み)。
    writes_passed: tuple[str, ...] = ()
    note: str = ""

    def reached(self) -> ResumeStage:
        """The single stage the run actually reached. **報告はこれだけを見る。**

        単調性が破れる状態は嘘なので、報告せず例外にする
        (:meth:`recon.observe_api.ApiObservation.reached` と同じ規律)。

        **描画は鎖に載せない。** 「行が現れた」と「応答を聴けた」に前後関係が
        無いのは ``observe-api`` と同じだが、こちらは **押すために行が要る** ので
        「行が現れた」は鎖に載る。載せてよいのは、前を通らないと後が始まらない
        工程だけである -- 行が無ければ押す対象が無い。
        """
        chain: tuple[tuple[ResumeStage, bool], ...] = (
            (ResumeStage.NO_SESSION, self.session_present),
            (ResumeStage.SESSION_EXPIRED, self.session_present and not self.session_expired),
            (ResumeStage.NO_ROWS, self.list_rendered),
            (ResumeStage.NOTHING_PRESSED, any(a.pressed for a in self.attempts)),
            (ResumeStage.NOTHING_NEW_HEARD, bool(self.after)),
        )
        stopped: ResumeStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                raise ValueError(
                    f"ResumeObservation の状態が時系列と矛盾しています: {stopped.value} で"
                    f"止まったのに {stage.value} を通過した証拠がある。"
                    " どこかがブール値を事実と違えて立てています (報告を嘘にしないため停止)。"
                )
        return stopped or ResumeStage.HEARD

    def resume_candidates(self) -> tuple[ObservedCall, ...]:
        """Calls whose response **shape** looks like a resume. Never auto-picked."""
        return tuple(call for call in self.after if looks_like_a_resume(call))

    def render(self) -> str:
        lines = ["段階3: レジュメ取得APIの形 (**送信は遮断してあります**)", ""]
        stage = self.reached()

        if stage is ResumeStage.NO_SESSION:
            lines.append("  保存セッションがありません。段階1からやり直してください。")
            return "\n".join(lines)
        if stage is ResumeStage.SESSION_EXPIRED:
            lines.append("  セッションが切れています (ログイン画面が出ました)。")
            lines.append("  Copy as cURL からシークレットを取り直してください。")
            return "\n".join(lines)

        lines.append(
            f"  一覧を開いた時点の応答: {len(self.before)} 件"
            f" / 聴く仕掛け: {'張れました' if self.listener_attached else '**張れませんでした**'}"
        )
        lines.extend(self._attempt_lines())
        lines.extend(self._write_lines())

        if stage is ResumeStage.NO_ROWS:
            lines.append("")
            lines.append(f"  **一覧の行が現れませんでした。** {self.note}")
            lines.append("  押す対象が無いので、レジュメは観測できていません。")
            lines.extend(self._blocked_lines())
            return "\n".join(lines)
        if stage is ResumeStage.NOTHING_PRESSED:
            lines.append("")
            lines.append("  **1つも押せませんでした。** 上の失敗理由を見てください。")
            lines.extend(self._blocked_lines())
            return "\n".join(lines)
        if stage is ResumeStage.NOTHING_NEW_HEARD:
            lines.append("")
            lines.append("  **押しましたが、新しい応答は1つも届きませんでした。**")
            lines.append("  押した部品がレジュメを開くものではなかった可能性があります。")
            lines.append("  遮断が止めた通信 (下) に GraphQL が居れば、それが本命です。")
            lines.extend(self._blocked_lines())
            return "\n".join(lines)

        lines.append("")
        lines.append(f"押した後に **新しく** 届いた応答: {len(self.after)} 件")
        lines.append("")
        for call in self.after:
            lines.append(call.render())
            lines.append("")

        lines.append("config/site_coordinates.yaml の該当行:")
        lines.append("")
        lines.extend(self._coordinate_lines())
        lines.append("")
        lines.extend(self._blocked_lines())
        lines.append("")
        lines.append("**送信は遮断されています** (GraphQL の mutation は1つも通していません)。")
        lines.append(
            "応答の値は1つも出していません。出したのはキーの名前と値の種別だけです (13.2)。"
        )
        return "\n".join(lines)

    def _attempt_lines(self) -> list[str]:
        out = [f"  押した部品: {len(self.attempts)} 個"]
        for attempt in self.attempts:
            if attempt.pressed:
                out.append(
                    f"    押せた: {attempt.selector} (新しい応答 {attempt.new_responses} 件)"
                )
            else:
                out.append(f"    押せなかった: {attempt.selector} -- {attempt.failure}")
        return out

    def _write_lines(self) -> list[str]:
        """**何を書いたかを黙らない。** 0件でも書く (原則2)。"""
        if not self.accepted_a_write:
            return ["  受け入れた書き込み: なし (遮断は厳しいまま)"]
        out = [
            "  **1回目でレジュメが取れなかったので、書き込みを1つ受け入れて再試行しました。**",
            f"  受け入れた対象: {KNOWN_WRITE_MARK_READ} "
            "(プロフィールを「確認済み」にする。運用者が手で開くのと同じ)",
        ]
        if self.writes_passed:
            out.append(f"  実際に通した書き込み: {len(self.writes_passed)} 件")
            out.extend(f"    {entry}" for entry in self.writes_passed)
        else:
            out.append("  実際には1件も飛びませんでした。")
        return out

    def _blocked_lines(self) -> list[str]:
        """**0件でも書く** (原則2)。黙ると「観測しなかった」と区別が付かない。"""
        if not self.blocked:
            return ["", "遮断が止めた通信: 0 件。"]
        out = ["", f"遮断が **止めた** 通信: {len(self.blocked)} 件"]
        seen: list[str] = []
        for entry in self.blocked:
            if entry not in seen:
                seen.append(entry)
        out.extend(f"  {entry}" for entry in seen[:20])
        return out

    def _coordinate_lines(self) -> list[str]:
        found = self.resume_candidates()
        if not found:
            return [
                f"  api.resume.url_pattern: {UNRESOLVED_TOKEN}",
                "    # **レジュメらしい応答は見つかりませんでした。**",
                "    # 上の応答一覧を見て、レジュメの項目が載っているものを選んでください。",
            ]
        out = [
            f"  api.resume.url_pattern: {UNRESOLVED_TOKEN}",
            "    # レジュメらしいキーを持つ応答 (**機械は1つに決めません** -- 原則3):",
        ]
        seen: list[str] = []
        for call in found:
            label = f"{call.operation}  {call.redacted_url}".strip()
            if label not in seen:
                seen.append(label)
                out.append(f"    #   {label}")
        out.append("    # 実画面の項目 (会員番号/年齢/居住地/最終学歴/資格/経験職種/")
        out.append("    # 希望職種/希望勤務地/希望勤務形態/希望年収/こだわり条件/自己PR)")
        out.append("    # が揃っているものを選んで貼ってください。")
        return out


def looks_like_a_resume(call: ObservedCall) -> bool:
    """Whether a response's key **names** look like a resume. **Pure.**

    名前で当たりを付けるだけである。当たったからといって座標に書いてよい
    わけではない -- 決めるのは人間である (原則3)。
    """
    if call.unread_reason or not call.keys:
        return False
    hit = {hint for hint in RESUME_KEY_HINTS if _mentions(call.keys, hint)}
    # **1つ当たっただけでは名乗らせない。** 一覧の応答にも desired や scouted は
    # 出るので、1語でレジュメ扱いすると本命が候補に埋もれる。
    return len(hit) >= 3


def _mentions(keys: Sequence[KeyPath], hint: str) -> bool:
    flat = hint.replace("_", "")
    return any(flat in path.path.lower().replace("_", "") for path in keys)


#: 押しても何も開かない部品。**押す価値が無く、状態だけ変える。**
#:
#: 実測25回目、探索は最初にチェックボックスを2回押した (``label`` と ``input``)。
#: どちらも新しい応答は0件で、変わったのは候補者の選択状態だけである。
#: 押す予算 (:data:`MAX_PRESSES`) をここで使い切ると本命に届かない。
NON_OPENING_CLASS_HINTS: tuple[str, ...] = ("checkbox", "radio", "toggle", "switch")


def _cannot_open_anything(candidate: ActionCandidate) -> bool:
    """Whether this control only flips state. **Pure.**"""
    haystack = " ".join(candidate.tokens).lower()
    return any(hint in haystack for hint in NON_OPENING_CLASS_HINTS)


def _pressable(candidates: Sequence[ActionCandidate]) -> tuple[ActionCandidate, ...]:
    """Controls we are willing to press. **閉じる・禁止・無効は外す。**

    送信らしい部品を外していないのは、遮断が許可制で送信を止めているから
    である。むしろ外すと「プロフィールを開く」導線が送信フォームの中にある
    場合に届かなくなる (実画面では送信フォームの左半分がレジュメである)。
    """
    return tuple(
        candidate
        for candidate in candidates
        if not is_closing(candidate)
        and not is_forbidden(candidate)
        and not is_disabled(candidate)
        and not _cannot_open_anything(candidate)
    )


def observe_resume(
    config: BrowserConfig,
    credentials_dir: Path,
    candidate_list_url: Coord[str],
    list_ready_selector: Coord[str],
) -> ResumeObservation:
    """Open a candidate's profile with the send blocked, and read the shape.

    **武装は遷移より前である。** 押すコマンドなので、順序を間違えれば取り消せない
    送信が起きうる。武装はループの外側で行い、``finally`` で解除する
    (:mod:`recon.capture_open` と同じ規律)。
    """
    requested_url = require(candidate_list_url, used_by="recon.observe_resume.observe_resume")
    ready_selector = require(list_ready_selector, used_by="recon.observe_resume.observe_resume")
    row_token = ready_selector.split(",")[0].strip()

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return ResumeObservation(requested_url=requested_url, session_present=False)

    strict = _attempt(config, session, requested_url, row_token, accepted_writes=frozenset())
    if strict.reached() is not ResumeStage.HEARD or not strict.resume_candidates():
        # **1回目で取れなかったときだけ、書き込みを1つ受け入れて2回目を行う。**
        #
        # 実測25回目、遮断は ``members/mark_read`` を止めた。プロフィールを開くと
        # 飛ぶ書き込みで、止めた結果モーダルはレジュメの取得まで進まなかった --
        # 止めたことが観測を殺した (実測22回目と同じ形である)。
        #
        # **受け入れてよい根拠**: これは運用者が「プロフィール確認」を押すたびに
        # 起きている書き込みそのもので、立てるのは一覧の ``read_profile`` だけで
        # ある。送信でも、枠の消費でも、候補者へ届くものでもない (13.6 が守って
        # いるのは送信である)。**それでも書き込みなので、報告に必ず出す。**
        second = _attempt(
            config,
            session,
            requested_url,
            row_token,
            accepted_writes=frozenset({KNOWN_WRITE_MARK_READ}),
        )
        return second
    return strict


def _attempt(
    config: BrowserConfig,
    session: Path,
    requested_url: str,
    row_token: str,
    *,
    accepted_writes: frozenset[str],
) -> ResumeObservation:
    """One pass: open the list, press, listen. **武装は遷移より前。**"""
    gate = SendGate(mode=GateMode.BLOCK_SEND, accepted_writes=accepted_writes)
    listener = ResponseShapeListener()
    accepted_a_write = bool(accepted_writes)
    with browser_context(config, storage_state=session) as (_context, page):
        install_gate(page, gate)
        attached = False
        try:
            page.on("response", listener.hear)
            attached = True
        except Exception:  # noqa: BLE001 -- 張れなかったことを記録して続ける
            attached = False
        # **押す前に武装する。** ここが唯一破ってはいけない不変条件である。
        gate.arm()
        try:
            goto(page, requested_url, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            if login_form_visible(page, config.selector_timeout_ms):
                return ResumeObservation(
                    requested_url=requested_url,
                    session_expired=True,
                    listener_attached=attached,
                    accepted_a_write=accepted_a_write,
                )
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            rendered = marker_present(page, row_token, timeout_ms=config.selector_timeout_ms)
            before = tuple(listener.calls)
            if not rendered:
                return ResumeObservation(
                    requested_url=requested_url,
                    before=before,
                    blocked=_blocked(gate),
                    listener_attached=attached,
                    accepted_a_write=accepted_a_write,
                    writes_passed=_writes(gate),
                    note=f"行 ({row_token}) が現れませんでした。",
                )

            attempts = _press_until_something_arrives(page, config, listener, row_token)
            return ResumeObservation(
                requested_url=requested_url,
                list_rendered=True,
                before=before,
                after=tuple(listener.calls[len(before) :]),
                attempts=attempts,
                blocked=_blocked(gate),
                listener_attached=attached,
                accepted_a_write=accepted_a_write,
                writes_passed=_writes(gate),
            )
        finally:
            gate.disarm()


def _blocked(gate: SendGate) -> tuple[str, ...]:
    return tuple(f"{entry.method} {redact_url(entry.url)}" for entry in gate.recorded)


def _writes(gate: SendGate) -> tuple[str, ...]:
    return tuple(f"{entry.method} {redact_url(entry.url)}" for entry in gate.accepted_passed)


def _press_until_something_arrives(
    page: Any,
    config: BrowserConfig,
    listener: ResponseShapeListener,
    row_token: str,
) -> tuple[PressAttempt, ...]:
    """Press card controls until new responses arrive, or the budget runs out."""
    tree = dom_tree(page)
    if tree is None:
        return (PressAttempt(selector=row_token, pressed=False, failure="DOMを読めませんでした"),)
    rows = indices_with_token(tree, row_token)
    if not rows:
        # **描画は確認済みなのに木に無い。** 黙って0件にしない (原則2)。
        return (
            PressAttempt(
                selector=row_token,
                pressed=False,
                failure="行は現れたのにDOMの木に見つかりませんでした",
            ),
        )
    candidates = _pressable(card_action_candidates(tree, subtree_sizes(tree), rows[0]))
    attempts: list[PressAttempt] = []
    for candidate in candidates[:MAX_PRESSES]:
        seen_before = len(listener.calls)
        pressed, failure = _press(page, candidate.selector(), config)
        if pressed:
            wait_for_content_to_arrive(page, config.selector_timeout_ms)
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
        arrived = len(listener.calls) - seen_before
        attempts.append(
            PressAttempt(
                selector=candidate.selector(),
                pressed=pressed,
                new_responses=arrived,
                failure=failure,
            )
        )
        if any(looks_like_a_resume(call) for call in listener.calls[seen_before:]):
            # **レジュメが届いたら止める。**
            #
            # 実測25回目はここが「何か届いたら止める」だった。最初に届いたのは
            # ``members/mark_read`` の (遮断が差し替えた) 空の応答で、そこで
            # 打ち切ったせいでレジュメを待たずに終わった。**「届いた」は
            # 「取れた」ではない** (実測18回目と同じ形の間違いである)。
            break
    return tuple(attempts)


def _press(page: Any, selector: str, config: BrowserConfig) -> tuple[bool, str]:
    """Press one control. Returns ``(pressed, failure_reason)``.

    実測18回目のとおり、通常のクリックが通らない画面があるので
    ``dispatch_event`` を控えに置く。**「届いた」は「効いた」ではない** ので、
    どちらで届いたかは呼び出し側には返さない -- 効いたかどうかは応答が来たか
    で判断する。
    """
    element = None
    with suppress(Exception):
        element = page.query_selector(selector)
    if element is None:
        return False, "要素が見つかりませんでした"
    try:
        element.click(timeout=config.selector_timeout_ms)
        return True, ""
    except Exception:  # noqa: BLE001 -- 生のメッセージは出さない (13.2)
        pass
    try:
        element.dispatch_event("click")
        return True, ""
    except Exception:  # noqa: BLE001
        return False, "クリックが届きませんでした"


__all__ = [
    "MAX_PRESSES",
    "OBSERVE_RESUME_KEYS",
    "RESUME_KEY_HINTS",
    "PressAttempt",
    "ResumeObservation",
    "ResumeStage",
    "looks_like_a_resume",
    "observe_resume",
]
