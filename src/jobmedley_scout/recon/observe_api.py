"""Stage-3 reconnaissance: watch the platform's **read** APIs. **押さない。**

段階3で送信路は取れた。残っているのは読み取り側で、そこに **送信を止めている値**
がある::

    観測した送信 payload:
      variables.input = { jobOfferId, jobOfferSalaryId, memberId,
                          scoutMessage, searchUuid }

``searchUuid`` は「どの検索から辿り着いた候補者か」を指す。**一覧の応答から
持ち出せなければ、送信は組み立てられない。** その出所はまだ見ていない。

見ていない理由は単純で、**一覧は武装より前に読み込まれる** からである。
既存の偵察 (``follow-send`` / ``capture-open``) は一覧が描画されてから武装する
ので、一覧そのものの通信は記録に入らない。

このコマンドは順序を変える。**最初から仕掛け、応答を聴きながら一覧を開く。**

**ただし媒体のオリジンは止めない** (:data:`~recon.gate.GateMode.BLOCK_THIRD_PARTY`)。
実測22回目、書き込みを全部止めたまま一覧を開いたら、候補者を取ってくる通信
そのものが止まった::

    POST /api/customers/customer_search_conditions/search_manual/
    POST /api/customers/received_favorites/search/
    POST /api/customers/scouted_members/search/

**この媒体の読み取りは GraphQL ではなく REST の POST である。** 遮断から見れば
書き込みと区別が付かない。観測したかったものを、観測のための仕掛けが止めていた。

**押さない。そしてそれがこのコマンドの安全性そのものである。**

媒体のオリジンを素通しにする以上、遮断はもう送信を止めない (送信APIも同じ
オリジンにある)。守っているのは「押さないこと」で、それは試験で固定してある
(``tests/guardrails/test_observe_only_never_presses.py``)。

押さなければ、飛ぶ通信は **運用者が自分でそのページを開いたときと同じもの**
だけになる。偵察が新たな副作用を持ち込むことはない。

**値は1文字も出さない** (13.2)。一覧の応答には氏名・会員番号・年齢・居住地が
入っている。出すのはキーの名前と値の種別だけで、判定は :mod:`recon.api_shape`
(純粋) が行う。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from jobmedley_scout.browser import session_store
from jobmedley_scout.browser.context import browser_context
from jobmedley_scout.browser.dom import (
    login_form_visible,
    wait_for_interactive,
    wait_for_structure_to_settle,
)
from jobmedley_scout.browser.navigation import goto, marker_present
from jobmedley_scout.config.placeholders import UNRESOLVED_TOKEN, Coord, require
from jobmedley_scout.config.schema import BrowserConfig
from jobmedley_scout.recon.api_shape import (
    ObservedCall,
)
from jobmedley_scout.recon.capture_send import install_gate
from jobmedley_scout.recon.gate import GateMode, SendGate
from jobmedley_scout.recon.listen import ResponseShapeListener as _Listener
from jobmedley_scout.recon.open_structure import redact_url

#: このコマンドが埋めうる座標キー。
OBSERVE_API_KEYS: tuple[str, ...] = (
    "api.candidate_list.url_pattern",
    # 応答だけでなく **要求本文の形も出す** ので、雛形もここで埋まる。
    "api.candidate_list.payload_template",
    "api.resume.url_pattern",
)


def _group(calls: Sequence[ObservedCall]) -> tuple[tuple[ObservedCall, int], ...]:
    """Collapse repeats, keeping order and counting them. **Pure.**

    単一ページアプリは同じ数本を何度も取り直す。1回ずつ並べると報告が同じ行で
    埋まり、本命が見つけられなくなる (実測22回目: 同じ4本で90行)。
    **回数は落とさない** -- 何度も取り直していること自体が観測だからである。
    """
    order: list[ObservedCall] = []
    counts: dict[tuple[str, str, str], int] = {}
    for call in calls:
        key = (call.operation, call.redacted_url, call.method)
        if key not in counts:
            counts[key] = 0
            order.append(call)
        counts[key] += 1
    return tuple((call, counts[(call.operation, call.redacted_url, call.method)]) for call in order)


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    """Deduplicate while keeping order. **同じ行を90回出さない。**

    実測22回目、単一ページアプリが同じ4本を何十回も取り直したせいで、座標の
    候補一覧が同じURLで90行埋まった。読む人が本命を見つけられない報告は、
    出していないのと大差ない。
    """
    seen: set[str] = set()
    kept: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            kept.append(item)
    return tuple(kept)


class ApiStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    NO_SESSION = "no_session"
    SESSION_EXPIRED = "session_expired"
    NOTHING_HEARD = "nothing_heard"
    HEARD = "heard"


@dataclass(frozen=True)
class ApiObservation:
    """The whole run, in the shape the report needs."""

    requested_url: str
    landed_url: str = ""
    session_present: bool = True
    session_expired: bool = False
    list_rendered: bool = False
    calls: tuple[ObservedCall, ...] = ()
    #: 一覧を開いている間に飛んで、**他所へ行くので止めた** 通信 (URLは伏せ済み)。
    #: ほとんどが計測ビーコンである。
    blocked_on_load: tuple[str, ...] = ()
    #: 一覧を開いている間に **媒体のオリジンへ実際に飛んだ** 非GET (URLは伏せ済み)。
    #:
    #: **止めていない。** 押していないので、これは運用者が自分でページを開いた
    #: ときに飛ぶものと同じである。ここに候補者一覧の取得が含まれる。
    passed_on_load: tuple[str, ...] = ()
    #: 応答を聴く仕掛けが実際に張れたか。**張れなかったことを黙らない。**
    #:
    #: 仕掛けは ``suppress(Exception)`` の中で張っている。失敗しても実行は続く
    #: ので、**1件も聴けなかったときに「応答が無かった」と区別が付かない**。
    listener_attached: bool = False
    #: 媒体のオリジンではなかったので聴かなかった応答の数。
    ignored: int = 0
    #: 読める種別ではなかったので本文を見なかった応答の数。
    skipped_binary: int = 0
    note: str = ""

    def reached(self) -> ApiStage:
        """The single stage the run actually reached. **報告はこれだけを見る。**

        工程を時系列で1本の鎖にし、最初に False になる工程を到達点とする。
        単調性が破れる状態は嘘なので、報告せず例外にする
        (:meth:`recon.follow_send.SendWalk.reached` と同じ規律)。

        **描画は鎖に載せない。** 最初こう書いて、偽のページで即座に落ちた --
        「一覧が描画された」と「応答を聴けた」は **前後関係が無い** からである。

        - 応答は遷移の最中に届く。行が現れる前に聴き終わっていることがある
        - 検索結果が0件でも、エラーでも、応答そのものは届く (行は現れない)
        - 逆に、描画がキャッシュから行われれば、行は現れるのに応答は来ない

        独立した2つの事実を1本の鎖に並べると、**正常な実行が「嘘」として例外に
        なる**。鎖に載せてよいのは、前を通らないと後が始まらない工程だけである。
        描画したかどうかは、聴いた内容がどこまで信用できるかを添えるための
        事実として、報告に併記する。
        """
        chain: tuple[tuple[ApiStage, bool], ...] = (
            (ApiStage.NO_SESSION, self.session_present),
            (ApiStage.SESSION_EXPIRED, self.session_present and not self.session_expired),
            (ApiStage.NOTHING_HEARD, bool(self.calls)),
        )
        stopped: ApiStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                raise ValueError(
                    f"ApiObservation の状態が時系列と矛盾しています: {stopped.value} で"
                    f"止まったのに {stage.value} を通過した証拠がある。"
                    " どこかがブール値を事実と違えて立てています (報告を嘘にしないため停止)。"
                )
        return stopped or ApiStage.HEARD

    def search_id_candidates(self) -> tuple[str, ...]:
        """Every key path that might carry the send payload's ``searchUuid``."""
        found: list[str] = []
        for call in self.calls:
            for path in call.search_id_candidates():
                label = f"{call.operation}: {path}"
                if label not in found:
                    found.append(label)
        return tuple(found)

    def render(self) -> str:
        lines = ["段階3: 読み取りAPIの形 (**押していません**)", ""]
        stage = self.reached()

        if stage is ApiStage.NO_SESSION:
            lines.append("  保存セッションがありません。段階1からやり直してください。")
            return "\n".join(lines)
        if stage is ApiStage.SESSION_EXPIRED:
            lines.append("  セッションが切れています (ログイン画面が出ました)。")
            lines.append("  Copy as cURL からシークレットを取り直してください。")
            return "\n".join(lines)
        if stage is ApiStage.NOTHING_HEARD:
            # **「静かなゼロ件」を名指しし、切り分けに要る数を全部出す** (原則2)。
            lines.append("  **媒体のオリジンからの応答を1つも聴けませんでした。**")
            lines.extend(self._heard_counts())
            if not self.listener_attached:
                lines.append("  → **聴く仕掛けが張れていません。** 応答の有無以前の問題です。")
            elif self.ignored:
                lines.append("  → 他所のオリジンの応答は届いています。媒体だけが無言でした。")
            else:
                lines.append("  → 応答が1つも届いていません。遷移そのものが起きていない可能性。")
            lines.extend(self._blocked_lines())
            return "\n".join(lines)

        grouped = _group(self.calls)
        lines.append(f"  聴いた応答: {len(self.calls)} 件 ({len(grouped)} 種類)")
        lines.extend(self._heard_counts())
        if not self.list_rendered:
            # **描画していないことを黙らない。** 聴けた応答が、一覧を出すための
            # ものだったかどうかが疑わしくなる (検索が0件だった、エラーだった等)。
            lines.append(f"  ただし **一覧の行は現れませんでした**。{self.note}")
            lines.append("  聴いた応答が一覧の取得だったかは、キーの形で判断してください。")
        lines.append("")
        # **同じ通信を何十回も並べない。** 単一ページアプリは同じ数本を取り直す。
        # 実測22回目の報告は、同じ4本で90行埋まっていた。
        for call, times in grouped:
            lines.append(call.render())
            if times > 1:
                lines.append(f"    (同じ通信が {times} 回ありました)")
            lines.append("")

        lines.append("config/site_coordinates.yaml の該当行:")
        lines.append("")
        lines.extend(self._coordinate_lines())
        lines.append("")
        lines.extend(self._blocked_lines())
        lines.append("")
        lines.append("**このコマンドはボタンを1つも押していません。**")
        lines.append(
            "応答の値は1つも出していません。出したのはキーの名前と値の種別だけです (13.2)。"
        )
        return "\n".join(lines)

    def _heard_counts(self) -> list[str]:
        """The numbers that tell "nothing was there" from "nothing was looked at"."""
        return [
            f"  (聴く仕掛け: {'張れました' if self.listener_attached else '**張れませんでした**'}"
            f" / 他所のオリジンなので聴かなかった応答: {self.ignored} 件"
            f" / 読めない種別なので本文を見なかった応答: {self.skipped_binary} 件)",
        ]

    def _blocked_lines(self) -> list[str]:
        """**0件でも書く** (原則2)。黙ると「観測しなかった」と区別が付かない。"""
        out: list[str] = []
        # **媒体へ実際に飛んだもの。** ここに一覧の取得が居る。
        if self.passed_on_load:
            out.append(
                f"一覧を開いている間に媒体のオリジンへ飛んだ非GET: "
                f"{len(self.passed_on_load)} 件 "
                f"(**止めていません** -- 押していないので、"
                f"運用者がページを開いたときと同じ通信です)"
            )
            out.extend(f"  {url}" for url in _unique(self.passed_on_load)[:20])
        else:
            out.append("一覧を開いている間に媒体のオリジンへ飛んだ非GET: 0 件。")
        # **他所へ行くので止めたもの。** 観測には要らない。
        out.append(
            f"他所のオリジンへ行こうとして **止めた** 通信: "
            f"{len(self.blocked_on_load)} 件 (計測ビーコン等。媒体とは無関係です)"
        )
        return out

    def _coordinate_lines(self) -> list[str]:
        out: list[str] = []
        listing = [c for c in self.calls if c.keys and not c.unread_reason]
        if listing:
            # **1件に決め打ちしない。** どれが一覧の取得かは操作名だけでは
            # 断定できないので、候補を並べて人間に選ばせる (原則3)。
            out.append(f"  api.candidate_list.url_pattern: {UNRESOLVED_TOKEN}")
            out.append("    # 聴いた読み取りの候補 (操作名とURL。**同じものはまとめてあります**):")
            for label in _unique(
                f"{call.operation}  {call.redacted_url}".strip() for call in listing
            ):
                out.append(f"    #   {label}")
            out.append("    # 候補者の並びを返しているものを選んで貼ってください。")
            out.append("    # **どれか1つを機械が選ぶことはしません** -- 応答の値を")
            out.append("    # 見ないと決められず、値は見ない方針だからです (13.2/原則3)。")
        else:
            out.append(f"  api.candidate_list.url_pattern: {UNRESOLVED_TOKEN}")
            out.append("    # 読める応答が1つもありませんでした。")

        if found := self.search_id_candidates():
            out.append("")
            out.append("  # **送信に要る searchUuid の出所らしいキー**:")
            for label in found:
                out.append(f"  #   {label}")
            out.append("  # 名前が似ていることは、同じ値であることの証明ではありません。")
            out.append("  # 段階4で送信payloadの値と突き合わせて確かめてください (原則3)。")
        else:
            out.append("")
            out.append("  # **searchUuid らしいキーは見つかりませんでした。**")
            out.append("  # 送信payloadには載っているので、どこかから来ています --")
            out.append("  # URLのクエリ、ページのHTML、別の通信のいずれかです。")
        return out


def observe_api(
    config: BrowserConfig,
    credentials_dir: Path,
    candidate_list_url: Coord[str],
    list_ready_selector: Coord[str],
) -> ApiObservation:
    """Open the list with the gate armed **from the start**, and listen.

    **武装の位置がこのコマンドの要点である。**

    ``follow-send`` は一覧が描画されてから武装する。押す直前まで素通しにする
    ためで、それは正しい -- が、その順序では **一覧そのものの通信が記録に入らない**。

    こちらは最初から武装する。書き込み (mutation・その他の非GET) は1つも通らず、
    読み取り (GraphQL の query) だけが通る。一覧の描画は読み取りで行われるので、
    武装したままでも画面は出る (実測5回目で分かった作り)。
    """
    requested_url = require(candidate_list_url, used_by="recon.observe_api.observe_api")
    ready_selector = require(list_ready_selector, used_by="recon.observe_api.observe_api")
    row_token = ready_selector.split(",")[0].strip()

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return ApiObservation(requested_url=requested_url, session_present=False)

    gate = SendGate(mode=GateMode.BLOCK_THIRD_PARTY)
    listener = _Listener()
    with browser_context(config, storage_state=session) as (_context, page):
        install_gate(page, gate)
        # **聴くのは遷移より前に仕掛ける。** 後だと最初の応答を取り逃す。
        attached = False
        try:
            page.on("response", listener.hear)
            attached = True
        except Exception:  # noqa: BLE001 -- 張れなかったことを記録して続ける
            attached = False
        # **最初から武装する。** 押さないので、武装したまま最後まで行く。
        # 止まるのは他所への通信 (計測ビーコン) だけで、媒体のオリジンは素通しする
        # -- そうしないと候補者を取ってくる通信まで止まる (実測22回目)。
        gate.arm()
        try:
            goto(page, requested_url, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            if login_form_visible(page, config.selector_timeout_ms):
                return ApiObservation(
                    requested_url=requested_url,
                    session_expired=True,
                    landed_url=redact_url(page.url),
                    listener_attached=attached,
                )
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            rendered = marker_present(page, row_token, timeout_ms=config.selector_timeout_ms)
            # 描画のあとにも遅れて届く応答がある。**もう一度落ち着くまで待つ。**
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            return ApiObservation(
                requested_url=requested_url,
                # **到達URLも伏せる。** 一覧から詳細へ落ちていれば会員IDが載る。
                landed_url=redact_url(page.url),
                list_rendered=rendered,
                calls=tuple(listener.calls),
                blocked_on_load=tuple(
                    f"{entry.method} {redact_url(entry.url)}" for entry in gate.recorded
                ),
                passed_on_load=tuple(
                    f"{entry.method} {redact_url(entry.url)}"
                    for entry in gate.passed_reads
                    if entry.method not in ("GET", "HEAD")
                ),
                listener_attached=attached,
                ignored=listener.ignored,
                skipped_binary=listener.skipped_binary,
                note="" if rendered else f"行 ({row_token}) が現れませんでした。",
            )
        finally:
            gate.disarm()


__all__ = ["OBSERVE_API_KEYS", "ApiObservation", "ApiStage", "observe_api"]
