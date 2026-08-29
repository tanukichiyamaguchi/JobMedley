"""求人IDを観測する。**押さない。送信しない。**

送信 payload に残る ``<...>`` のうち2つは、**実行時ではなく事前に決まる値**
である -- どの求人へのスカウトか (``jobOfferId``)、その求人のどの給与条件か
(``jobOfferSalaryId``)。運用者は求人を言葉で指定済みだが、**IDは画面に出て
こない**。一覧を開いたときに飛ぶ読み取りの応答にだけ入っている::

    GET /api/customers/job_offers/published/?limit=...
      data.job_offers[].id
      data.job_offers[].job_offer_salaries[].id

**このコマンドは値を出す。** 他の偵察コマンドは値を1文字も出さないが、ここは
違う。返ってくるのは **運用者自身が媒体へ公開している求人票** であり、13.2 が
守ろうとしている対象 (候補者の氏名・会員番号・年齢・居住地) ではない。値を
伏せると、どのIDがどの求人なのか運用者が突き合わせられず、**座標を埋める手段が
無くなる**。伏せる理由が無いところで伏せるのは、安全ではなく不便なだけである。

**出す範囲は絞ってある** (:func:`recon.job_offers.extract_job_offers`)。求人
オブジェクトの直下にある短い文字列だけを見出しにし、入れ子は辿らない。

``observe-api`` と同じく **最初から武装して開く**。押す操作が存在しないので、
飛ぶ通信は運用者が自分でそのページを開いたときと同じものだけになる。
"""

from __future__ import annotations

import json
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
from jobmedley_scout.recon.gate import GateMode, SendGate, is_own_origin
from jobmedley_scout.recon.job_offers import JobOffer, extract_job_offers, render_offers
from jobmedley_scout.recon.listen import MEDIA_HOST
from jobmedley_scout.recon.open_structure import redact_url

#: このコマンドが埋めうる座標キー。
OBSERVE_JOB_OFFERS_KEYS: tuple[str, ...] = (
    "api.send.paid.payload_template",  # variables.input.jobOfferId / jobOfferSalaryId
)

#: 求人一覧の読み取りを見分ける、URLパスの目印。**実測した経路そのもの。**
JOB_OFFERS_PATH: Final[str] = "/job_offers/published"

#: 運用者が指定した求人。一致は「絞れたかどうか」の報告にしか使わない。
WANTED_OFFER: Final[str] = "歯科衛生士"


@dataclass
class _OfferListener:
    """求人一覧の応答だけを読む。**それ以外は即座に捨てる。**"""

    offers: list[JobOffer]
    answered: bool = False
    read_failed: str = ""

    def hear(self, response: Any) -> None:
        url = ""
        with suppress(Exception):
            url = str(response.url)
        if not url or not is_own_origin(url, MEDIA_HOST):
            return
        if JOB_OFFERS_PATH not in url:
            return
        self.answered = True
        try:
            body = json.loads(response.text())
        except Exception as exc:  # noqa: BLE001 -- 読めなかったことを黙らない
            self.read_failed = type(exc).__name__
            return
        for offer in extract_job_offers(body):
            if all(offer.offer_id != seen.offer_id for seen in self.offers):
                self.offers.append(offer)


class OfferStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    NO_SESSION = "no_session"
    SESSION_EXPIRED = "session_expired"
    NOT_ANSWERED = "not_answered"
    NO_OFFERS = "no_offers"
    FOUND = "found"


@dataclass(frozen=True)
class OfferObservation:
    """The whole run, in the shape the report needs."""

    requested_url: str
    landed_url: str = ""
    session_present: bool = True
    session_expired: bool = False
    answered: bool = False
    read_failed: str = ""
    offers: tuple[JobOffer, ...] = ()
    blocked_on_load: tuple[str, ...] = ()
    listener_attached: bool = False

    def reached(self) -> OfferStage:
        """The single stage the run actually reached. **報告はこれだけを見る。**

        単調性が破れる状態は嘘なので、報告せず例外にする。後の段の条件は、
        前の段の条件に **含まれて** いなければならない。
        """
        chain: tuple[tuple[OfferStage, bool], ...] = (
            (OfferStage.NO_SESSION, self.session_present),
            (OfferStage.SESSION_EXPIRED, self.session_present and not self.session_expired),
            (OfferStage.NOT_ANSWERED, self.answered),
            (OfferStage.NO_OFFERS, bool(self.offers)),
        )
        stopped: OfferStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                raise ValueError(
                    f"OfferObservation の状態が時系列と矛盾しています: {stopped.value} で"
                    f"止まったのに {stage.value} を通過した証拠がある。"
                    " どこかがブール値を事実と違えて立てています (報告を嘘にしないため停止)。"
                )
        return stopped or OfferStage.FOUND

    def render(self) -> str:
        lines = ["段階4: 求人IDの観測 (**押していません。送信していません**)", ""]
        stage = self.reached()

        if stage is OfferStage.NO_SESSION:
            lines.append("  保存セッションがありません。段階1からやり直してください。")
            return "\n".join(lines)
        if stage is OfferStage.SESSION_EXPIRED:
            lines.append("  セッションが切れています (ログイン画面が出ました)。")
            lines.append("  Copy as cURL からシークレットを取り直してください。")
            return "\n".join(lines)

        lines.append(
            f"  聴く仕掛け: {'張れました' if self.listener_attached else '**張れませんでした**'}"
        )
        if stage is OfferStage.NOT_ANSWERED:
            # **「静かなゼロ件」を名指しする** (原則2)。
            lines.append(f"  **{JOB_OFFERS_PATH} の応答を1つも聴けませんでした。**")
            if not self.listener_attached:
                lines.append("  → 応答の有無以前に、聴く仕掛けが張れていません。")
            else:
                lines.append("  → 一覧を開いてもこの読み取りが飛ばない作りに変わった可能性。")
                lines.append("  → 求人が0件なのか、経路が違うのかは、この報告では決まりません。")
            lines.extend(self._blocked_lines())
            return "\n".join(lines)

        lines.append(f"  {JOB_OFFERS_PATH} の応答: 届きました")
        if self.read_failed:
            lines.append(f"  ただし **本文が読めませんでした** ({self.read_failed})。")
        lines.append("")
        lines.append(render_offers(self.offers, wanted=WANTED_OFFER))
        lines.append("")
        lines.extend(self._coordinate_lines())
        lines.append("")
        lines.extend(self._blocked_lines())
        lines.append("")
        lines.append("**このコマンドはボタンを1つも押しておらず、送信も1件もしていません。**")
        return "\n".join(lines)

    def _coordinate_lines(self) -> list[str]:
        out = ["config/site_coordinates.yaml の該当箇所:", ""]
        out.append("  api.send.paid.payload_template の variables.input:")
        out.append('    "jobOfferId":       上で選んだ jobOfferId')
        out.append('    "jobOfferSalaryId": その求人の jobOfferSalaryId')
        out.append("  # 残る2つ (memberId / searchUuid) は実行時に一覧の応答から取ります。")
        out.append("  # **どれを使うかを機械が決めることはしません** (原則3)。")
        return out

    def _blocked_lines(self) -> list[str]:
        """**0件でも書く** (原則2)。黙ると「観測しなかった」と区別が付かない。"""
        out = [f"止めた通信 (他所のオリジンへ): {len(self.blocked_on_load)} 件"]
        out.extend(f"  {entry}" for entry in self.blocked_on_load)
        return out


def observe_job_offers(
    config: BrowserConfig,
    credentials_dir: Path,
    candidate_list_url: Coord[str],
) -> OfferObservation:
    """Open the list with the gate armed **from the start**, and read the offers."""
    requested_url = require(
        candidate_list_url, used_by="recon.observe_job_offers.observe_job_offers"
    )

    session = session_store.session_path(credentials_dir)
    if not session.exists():
        return OfferObservation(requested_url=requested_url, session_present=False)

    gate = SendGate(mode=GateMode.BLOCK_THIRD_PARTY)
    listener = _OfferListener(offers=[])
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
                return OfferObservation(
                    requested_url=requested_url,
                    session_expired=True,
                    landed_url=redact_url(page.url),
                    listener_attached=attached,
                )
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            # 遅れて届く応答がある。**もう一度落ち着くまで待つ。**
            wait_for_structure_to_settle(page, config.selector_timeout_ms)
            return OfferObservation(
                requested_url=requested_url,
                landed_url=redact_url(page.url),
                answered=listener.answered,
                read_failed=listener.read_failed,
                offers=tuple(listener.offers),
                blocked_on_load=tuple(
                    f"{entry.method} {redact_url(entry.url)}" for entry in gate.recorded
                ),
                listener_attached=attached,
            )
        finally:
            gate.disarm()


__all__ = [
    "OBSERVE_JOB_OFFERS_KEYS",
    "OfferObservation",
    "OfferStage",
    "observe_job_offers",
]
