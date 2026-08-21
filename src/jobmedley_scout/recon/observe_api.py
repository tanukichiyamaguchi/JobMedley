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

このコマンドは順序を変える。**最初から武装し、応答を聴きながら一覧を開く。**

**押さない。** 一覧を開くだけで、カードのボタンには触れない。段階3で分かって
いるとおり、カードには送信を名乗るボタンが並んでいる (13.6: 送信は取り消せない)。
レジュメの形は、押さずに取れる範囲だけを報告する。

**値は1文字も出さない** (13.2)。一覧の応答には氏名・会員番号・年齢・居住地が
入っている。出すのはキーの名前と値の種別だけで、判定は :mod:`recon.api_shape`
(純粋) が行う。
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

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
    describe_response,
    operation_name,
    scan_text,
)
from jobmedley_scout.recon.capture_send import install_gate
from jobmedley_scout.recon.gate import GateMode, SendGate
from jobmedley_scout.recon.open_structure import redact_url

#: このコマンドが埋めうる座標キー。
OBSERVE_API_KEYS: tuple[str, ...] = (
    "api.candidate_list.url_pattern",
    "api.resume.url_pattern",
)

#: 聴く対象。**媒体自身のオリジンなら全部。**
#:
#: 最初は ``("job-medley.com", "graphql")`` の両方を求めていた。送信が GraphQL
#: だったので、読み取りもそうだろうと考えたからである。**実測21回目でそれが
#: 誤りだと分かった** -- 一覧は描画されたのに、聴けた応答は0件だった。
#:
#: 一覧のURLは ``/customers/searches?lg=0&da[0][pid]=14&...`` という長い問い合わせ
#: つきのページで、**サーバ側で組み立てられて返ってくる**。GraphQL は1本も飛ばない。
#:
#: 絞り込みを媒体のオリジンだけにする。計測ビーコンは他所のオリジンなので、
#: これでも入ってこない。
_MEDIA_HOST = "job-medley.com"

#: 本文を読んでよい応答の種別。画像や動画は読まない (読む意味が無く、重い)。
_READABLE_TYPES: tuple[str, ...] = ("json", "html", "javascript", "text/plain", "xml")


class ApiStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    NO_SESSION = "no_session"
    SESSION_EXPIRED = "session_expired"
    NOTHING_HEARD = "nothing_heard"
    HEARD = "heard"


@dataclass
class _Listener:
    """Collects response shapes as they arrive. **本文は溜めない。**

    応答が来たその場でキーの形に落として捨てる。生の本文を持ち回ると、例外の
    メッセージや保存経路から個人データが漏れる余地が増える (13.2)。

    **``browser.capture.ResponseBuffer`` を使わない理由。** あちらも
    ``page.on("response")`` を張る器で、返信検知 (10.4) のために書かれている。
    使わないのは2点で違うからである:

    * あちらは本文を **溜める** (40万字で切り詰めて窓が閉じるまで保持する)。
      返信の照合には本文が要るが、こちらは形しか要らない。**要らないものを
      持たない** ほうが、漏れる経路が少ない
    * あちらの ``measurement_window()`` は入場時と退場時に全部捨てる。
      10.4 の事故 (ブートストラップの応答が本番の照合に混ざった) を構文で
      塞ぐ設計で、正しいが、こちらの用途 (遷移中の全応答を残らず聴く) とは
      窓の切り方が合わない
    """

    calls: list[ObservedCall] = field(default_factory=list)
    #: 媒体のオリジンではなかったので聴かなかった応答の数。
    #:
    #: **必ず報告に出す。** 実測21回目、聴けた応答は0件だったが「いくつ無視したか」
    #: を出していなかったので、「本当に応答が無かった」のか「絞り込みが狭すぎた」
    #: のかが報告から決められなかった。数を出していれば即座に分かった --
    #: 自分で作った静かなゼロ件である。
    ignored: int = 0
    #: 読める種別ではなかったので本文を見なかった応答の数 (画像等)。
    skipped_binary: int = 0

    def hear(self, response: Any) -> None:
        url = ""
        with suppress(Exception):
            url = str(response.url)
        if not url:
            return
        if _MEDIA_HOST not in url.lower():
            self.ignored += 1
            return

        request_body: str | None = None
        method = "GET"
        with suppress(Exception):
            request_body = response.request.post_data
            method = str(response.request.method)

        content_type = ""
        with suppress(Exception):
            content_type = str(response.headers.get("content-type", ""))
        short_type = content_type.split(";", 1)[0].strip()

        if content_type and not any(kind in content_type.lower() for kind in _READABLE_TYPES):
            # 画像・動画・フォント。読む意味が無く、重い。**数だけ残す。**
            self.skipped_binary += 1
            return

        body: str | None = None
        reason = ""
        try:
            body = response.text()
        except Exception:  # noqa: BLE001 -- 生のメッセージは出さない (13.2)
            reason = "応答本文を取り出せませんでした"

        keys: tuple[Any, ...] = ()
        dropped = 0
        uuid_like = 0
        mentions = 0
        if not reason:
            keys, reason, dropped = describe_response(body)
            if reason:
                # JSON では無かった。**値は見ずに、形の数だけ数える。**
                uuid_like, mentions = scan_text(body)
        # **ここで本文を捨てる。** 以降どこにも残らない。
        body = None

        self.calls.append(
            ObservedCall(
                operation=operation_name(request_body),
                redacted_url=redact_url(url),
                method=method,
                keys=keys,
                unread_reason=reason,
                dropped_keys=dropped,
                content_type=short_type,
                uuid_like=uuid_like,
                send_key_mentions=mentions,
            )
        )


@dataclass(frozen=True)
class ApiObservation:
    """The whole run, in the shape the report needs."""

    requested_url: str
    landed_url: str = ""
    session_present: bool = True
    session_expired: bool = False
    list_rendered: bool = False
    calls: tuple[ObservedCall, ...] = ()
    #: 一覧を開いている間に飛んで、遮断が止めた書き込み (URLは伏せ済み)。
    #:
    #: **0件でも0件と書く。** これまでどのコマンドも、一覧のロード中は武装して
    #: いなかった (描画を殺さないため、押す直前に武装する設計)。つまり
    #: 「一覧を開くだけでは書き込みが飛ばない」は **一度も観測されていない**。
    #: このコマンドは最初から武装するので、ここで初めてそれが言える。
    blocked_on_load: tuple[str, ...] = ()
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

        lines.append(f"  聴いた応答: {len(self.calls)} 件")
        lines.extend(self._heard_counts())
        if not self.list_rendered:
            # **描画していないことを黙らない。** 聴けた応答が、一覧を出すための
            # ものだったかどうかが疑わしくなる (検索が0件だった、エラーだった等)。
            lines.append(f"  ただし **一覧の行は現れませんでした**。{self.note}")
            lines.append("  聴いた応答が一覧の取得だったかは、キーの形で判断してください。")
        lines.append("")
        for call in self.calls:
            lines.append(call.render())
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
        if self.blocked_on_load:
            out = [
                f"一覧を開いている間に飛んだ **書き込み**: {len(self.blocked_on_load)} 件 "
                f"(遮断が止めました。媒体のサーバへは到達していません)"
            ]
            out.extend(f"  {url}" for url in self.blocked_on_load[:10])
            return out
        return [
            "一覧を開いている間に飛んだ **書き込み**: 0 件。"
            "**開くだけでは何も書き込まれない**、と観測で言えました。"
        ]

    def _coordinate_lines(self) -> list[str]:
        out: list[str] = []
        listing = [c for c in self.calls if c.keys and not c.unread_reason]
        if listing:
            # **1件に決め打ちしない。** どれが一覧の取得かは操作名だけでは
            # 断定できないので、候補を並べて人間に選ばせる (原則3)。
            out.append(f"  api.candidate_list.url_pattern: {UNRESOLVED_TOKEN}")
            out.append("    # 聴いた読み取りの候補 (操作名とURL):")
            for call in listing:
                out.append(f"    #   {call.operation}  {call.redacted_url}")
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

    gate = SendGate(mode=GateMode.BLOCK_WRITES)
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
                listener_attached=attached,
                ignored=listener.ignored,
                skipped_binary=listener.skipped_binary,
                note="" if rendered else f"行 ({row_token}) が現れませんでした。",
            )
        finally:
            gate.disarm()


__all__ = ["OBSERVE_API_KEYS", "ApiObservation", "ApiStage", "observe_api"]
