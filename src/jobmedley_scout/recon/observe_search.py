"""一覧の **要求本文** を観測する。**押さない。送信しない。**

実測35回目 (``scout preview`` 1回目) で HTTP 500 が返った。座標
``api.candidate_list.payload_template`` が偵察の印のままだったからである --
40キーのうち差し込んでいたのは3つで、残る37キーは ``"<bool>"`` や ``"<number>"``
という **文字列** のまま媒体へ飛んでいた。

**この本文は推測で組み立てられない。** ``nav.candidate_list_url`` の問い合わせ
文字列に同じ条件が別の綴りで載ってはいる (``da[0][pid]`` / ``df[0]`` ...) が、
``da`` が ``desired_areas`` だというのは推測であり、当たったことを確かめる手段が
無い (原則3)。だから **一覧を開いて、媒体自身が送る本文をそのまま拾う**。

``observe-job-offers`` と同じく **最初から武装して開く**。押す操作が存在しない
ので、飛ぶ通信は運用者が自分でそのページを開いたときと同じものだけになる。

出す値について
--------------

**このコマンドは本文の値を出す。** 出るのは運用者自身の保存済み検索条件 (年齢の
範囲・希望エリア・職種・絞り込みフラグ) であって、候補者の情報ではない。13.2 が
守るのは候補者の氏名・会員番号・年齢・居住地であり、それらはこの本文に無い --
返ってくる **応答** の側にある。応答は1文字も読まない。

唯一の例外が ``member_id`` で、埋まっていれば候補者を名指しする。埋まっていたら
伏せる (:data:`recon.search_payload.WITHHELD_KEYS`)。
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jobmedley_scout.api.success import was_accepted
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
from jobmedley_scout.recon.gate import GateMode, SendGate, is_own_origin
from jobmedley_scout.recon.listen import MEDIA_HOST
from jobmedley_scout.recon.open_structure import redact_url
from jobmedley_scout.recon.search_payload import SearchTemplate, as_template

#: このコマンドが埋める座標キー。**この1個より多くも少なくも出さない。**
OBSERVE_SEARCH_KEYS: tuple[str, ...] = ("api.candidate_list.payload_template",)


def match_path(url_pattern: str) -> str:
    """The path to listen for, taken from the coordinate. **綴りを作らない。**

    ``api.candidate_list.url_pattern`` は既に観測で確定している。そこから経路を
    取れば、ここで ``/members/search`` と書き起こす必要が無い -- 書き起こせば、
    座標が変わったときに **黙って一致しなくなる** (原則2)。
    """
    path = urlsplit(url_pattern).path.rstrip("/")
    return path or url_pattern


@dataclass(frozen=True)
class CapturedPost:
    """One POST the page itself made. **本文と結果を対にして持つ。**"""

    status: int
    body: str

    def accepted(self) -> bool:
        """Whether the platform took this body. **判定は api/success.py に集めてある** (6.2)。"""
        return was_accepted(self.status)


@dataclass
class _SearchListener:
    """一覧APIへの POST だけを見る。**それ以外は即座に捨てる。**

    ``page.on("response")`` で聴く。要求と応答が対で手に入るからである --
    ``page.on("request")`` だと本文は取れるが、媒体がそれを受け付けたかどうかが
    分からない。**受け付けられた本文であることが、この観測の値打ちである。**
    """

    path: str
    posts: list[CapturedPost] = field(default_factory=list)

    def hear(self, response: Any) -> None:
        url = ""
        with suppress(Exception):
            url = str(response.url)
        if not url or not is_own_origin(url, MEDIA_HOST):
            return
        if self.path not in urlsplit(url).path:
            return
        body = ""
        status = 0
        with suppress(Exception):
            if str(response.request.method).upper() != "POST":
                return
            body = response.request.post_data or ""
            status = int(response.status)
        if not body:
            return
        self.posts.append(CapturedPost(status=status, body=body))

    def first_accepted(self) -> CapturedPost | None:
        """The first POST the platform accepted. **最初のものを採る。**

        画面が複数回引くことがある。最初の1回が素の読み込みで、後続は絞り込みの
        差し替えかもしれない。ページ番号と件数はどうせ差し込み記法に置き換える
        ので、素の読み込みを採るのが最も無難である。
        """
        return next((post for post in self.posts if post.accepted()), None)


class SearchStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    NO_SESSION = "no_session"
    SESSION_EXPIRED = "session_expired"
    NOT_ANSWERED = "not_answered"
    ALL_REJECTED = "all_rejected"
    UNREADABLE = "unreadable"
    FOUND = "found"


@dataclass(frozen=True)
class SearchObservation:
    """The whole run, in the shape the report needs."""

    requested_url: str
    listen_path: str
    landed_url: str = ""
    session_present: bool = True
    session_expired: bool = False
    posts: tuple[CapturedPost, ...] = ()
    captured: SearchTemplate | None = None
    blocked_on_load: tuple[str, ...] = ()
    listener_attached: bool = False

    def accepted_posts(self) -> tuple[CapturedPost, ...]:
        return tuple(post for post in self.posts if post.accepted())

    def reached(self) -> SearchStage:
        """The single stage the run actually reached. **報告はこれだけを見る。**

        単調性が破れる状態は嘘なので、報告せず例外にする。後の段の条件は、
        前の段の条件に **含まれて** いなければならない。
        """
        chain: tuple[tuple[SearchStage, bool], ...] = (
            (SearchStage.NO_SESSION, self.session_present),
            (SearchStage.SESSION_EXPIRED, self.session_present and not self.session_expired),
            (SearchStage.NOT_ANSWERED, bool(self.posts)),
            (SearchStage.ALL_REJECTED, bool(self.accepted_posts())),
            (SearchStage.UNREADABLE, self.captured is not None),
        )
        stopped: SearchStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                raise ValueError(
                    f"SearchObservation の状態が時系列と矛盾しています: {stopped.value} で"
                    f"止まったのに {stage.value} を通過した証拠がある。"
                    " どこかがブール値を事実と違えて立てています (報告を嘘にしないため停止)。"
                )
        return stopped or SearchStage.FOUND

    def render(self) -> str:
        lines = ["段階3: 一覧の要求本文の観測 (**押していません。送信していません**)", ""]
        stage = self.reached()

        if stage is SearchStage.NO_SESSION:
            lines.append("  保存セッションがありません。段階1からやり直してください。")
            return "\n".join(lines)
        if stage is SearchStage.SESSION_EXPIRED:
            lines.append("  セッションが切れています (ログイン画面が出ました)。")
            lines.append("  Copy as cURL からシークレットを取り直してください。")
            return "\n".join(lines)

        lines.append(
            f"  聴く仕掛け: {'張れました' if self.listener_attached else '**張れませんでした**'}"
            f" (経路 {self.listen_path} への POST)"
        )
        lines.append(f"  聴けた POST: {len(self.posts)} 件")
        lines.extend(f"    HTTP {post.status} ({len(post.body)} 字)" for post in self.posts)

        if stage is SearchStage.NOT_ANSWERED:
            # **「静かなゼロ件」を名指しする** (原則2)。
            lines.append("")
            lines.append(f"  **{self.listen_path} への POST を1つも聴けませんでした。**")
            if not self.listener_attached:
                lines.append("  → 通信の有無以前に、聴く仕掛けが張れていません。")
            else:
                lines.append("  → 一覧を開いてもこの経路が飛ばない作りに変わった可能性。")
                lines.append("  → 座標 api.candidate_list.url_pattern を確かめてください。")
            lines.extend(self._blocked_lines())
            return "\n".join(lines)
        if stage is SearchStage.ALL_REJECTED:
            lines.append("")
            lines.append("  **媒体が受け付けた POST が1つもありませんでした。**")
            lines.append("  画面自身の要求が通らないので、本文を写しても通りません。")
            lines.extend(self._blocked_lines())
            return "\n".join(lines)
        if stage is SearchStage.UNREADABLE or self.captured is None:
            lines.append("")
            lines.append("  **受け付けられた本文を JSON として読めませんでした。**")
            lines.append("  推測で雛形を作ることはしません (原則3)。")
            lines.extend(self._blocked_lines())
            return "\n".join(lines)

        lines.append("")
        lines.extend(self._template_lines(self.captured))
        lines.append("")
        lines.extend(self._blocked_lines())
        lines.append("")
        lines.append("**このコマンドはボタンを1つも押しておらず、送信も1件もしていません。**")
        return "\n".join(lines)

    def _template_lines(self, captured: SearchTemplate) -> list[str]:
        out = [
            f"観測した検索条件番号: "
            f"{captured.condition_id or '**取れませんでした**'}"
            f"  ← config.yaml の ingest.search_condition_id と一致しているか確認",
            "",
        ]
        if captured.withheld:
            out.append(f"  伏せた欄 (値が入っていた): {', '.join(captured.withheld)}")
            out.append("  → 候補者を名指ししうるので伏せました。運用者が判断して書いてください。")
        else:
            out.append("  伏せた欄: なし (member_id は空でした = 誰も名指ししていません)")
        out.append(f"  差し込み記法に置いた欄: {', '.join(captured.slotted) or 'なし'}")
        out.extend(self._drift_lines(captured))
        if not captured.usable():
            # **使えない雛形を「取れた」と言わない** (原則2)。
            out.append("")
            out.append(f"  **本文に無かった差し込み欄: {', '.join(captured.missing_slots)}**")
            out.append("  この雛形はこのままでは使えません。特に pagination.page が無いと、")
            out.append("  毎回1ページ目を引きながら報告だけがページを進みます。")
        out.append("")
        out.append("config/site_coordinates.yaml の api.candidate_list.payload_template に、")
        out.append("下の JSON を1行の文字列として貼ってください:")
        out.append("")
        out.extend(f"  {line}" for line in captured.template.splitlines())
        return out

    def _drift_lines(self, captured: SearchTemplate) -> list[str]:
        """前回の観測との差。**差が無いことも書く** (原則2)。

        絞り込みの欄が落ちても例外は出ない。出るのは「別の母集団を引いた」で
        あって、それは応答を見ても分からない。**判断は人がする** (原則3) --
        ここは差を並べるだけで、良し悪しを決めない。
        """
        if not captured.vanished_filters and not captured.new_filters:
            return ["  前回の観測との差: なし (絞り込みの欄は同じ)"]
        out: list[str] = []
        if captured.vanished_filters:
            out.append(f"  **前回に在って今回は無い欄**: {', '.join(captured.vanished_filters)}")
            out.append("  → 落ちたまま使うと、別の母集団を引いたまま気付きません。")
        if captured.new_filters:
            out.append(f"  前回に無かった欄: {', '.join(captured.new_filters)}")
        return out

    def _blocked_lines(self) -> list[str]:
        """**0件でも書く** (原則2)。黙ると「観測しなかった」と区別が付かない。"""
        out = [f"止めた通信 (他所のオリジンへ): {len(self.blocked_on_load)} 件"]
        out.extend(f"  {entry}" for entry in self.blocked_on_load)
        return out


def observe_search(
    config: BrowserConfig,
    credentials_dir: Path,
    candidate_list_url: Coord[str],
    search_api_url: Coord[str],
) -> SearchObservation:
    """Open the list with the gate armed **from the start**, and keep the POST body."""
    used_by = "recon.observe_search.observe_search"
    requested_url = require(candidate_list_url, used_by=used_by)
    listen_path = match_path(require(search_api_url, used_by=used_by))

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return SearchObservation(
            requested_url=requested_url, listen_path=listen_path, session_present=False
        )

    gate = SendGate(mode=GateMode.BLOCK_THIRD_PARTY)
    listener = _SearchListener(path=listen_path)
    with browser_context(config, storage_state=session) as (_context, page):
        install_gate(page, gate)
        attached = False
        try:
            page.on("response", listener.hear)
            attached = True
        except Exception:  # noqa: BLE001 -- 張れなかったことを記録して続ける
            attached = False
        gate.arm()
        try:
            goto(page, requested_url, config)
            wait_for_interactive(page, config.selector_timeout_ms)
            if login_form_visible(page, config.selector_timeout_ms):
                return SearchObservation(
                    requested_url=requested_url,
                    listen_path=listen_path,
                    session_expired=True,
                    landed_url=redact_url(page.url),
                    listener_attached=attached,
                )
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            # 遅れて届く応答がある。**もう一度落ち着くまで待つ。**
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            accepted = listener.first_accepted()
            return SearchObservation(
                requested_url=requested_url,
                listen_path=listen_path,
                landed_url=redact_url(page.url),
                posts=tuple(listener.posts),
                captured=as_template(accepted.body) if accepted else None,
                blocked_on_load=tuple(
                    f"{entry.method} {redact_url(entry.url)}" for entry in gate.recorded
                ),
                listener_attached=attached,
            )
        finally:
            gate.disarm()


__all__ = [
    "OBSERVE_SEARCH_KEYS",
    "CapturedPost",
    "SearchObservation",
    "SearchStage",
    "match_path",
    "observe_search",
]
