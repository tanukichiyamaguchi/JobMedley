"""**1通だけ送る。** 段階4-3 / 段階6 の1通目。

ここが取り返しのつかない唯一の場所である (13.6)。送ったものは取り消せず、相手の
受信箱に残り、月次の送信枠を1通消費する。だから門を3つ置いてある。

1. ``safety.dry_run`` が明示的に false であること
2. 呼び出し側が **取り消せないことを承知した印** を渡していること
3. 上限は **1件**。設定では変えられない (定数)

**成功ステータスはまだ確定していない。** ``api.send.paid.success_statuses`` は
「1通送らないと分からない」座標で、それを送る前に要求すると梯子が閉じる
(docs/ladder.md 4-3)。だからこの1通だけは **暫定で 2xx** を使い、そのことを
報告に明記する。GraphQL は失敗も 200 で返すので、判定の本体は
``errors`` と ``errorMessage`` のほうである (3本立て / api.success)。

**件名は媒体へ届かない。** 実測した送信 payload の入力欄は5つで、件名の欄が無い::

    jobOfferId / jobOfferSalaryId / memberId / scoutMessage / searchUuid

保存する件名は **手元の記録と返信突合のための札** であって、相手が目にするものでは
ない。10.2 は件名で返信を突き合わせる設計だが、**この媒体でそれが成立するかは
未確認である** (``inbox.*`` は未確定)。報告に必ず書く。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from jobmedley_scout.api.client import JobMedleyApiClient
from jobmedley_scout.api.endpoints import SEND_PAID, Endpoint
from jobmedley_scout.api.payloads import PLACEHOLDER_SEARCH_UUID
from jobmedley_scout.api.send import send_message
from jobmedley_scout.clock import Clock
from jobmedley_scout.config.schema import IngestConfig, LlmConfig, SafetyConfig
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.generation.llm_client import AnthropicLike
from jobmedley_scout.generation.scout_message import (
    GeneratedMessage,
    build_prompt,
    generate_scout_body,
)
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.models.message import AssembledMessage
from jobmedley_scout.models.send_record import MessageKind, SendResult, SendSlot
from jobmedley_scout.runtime.commands.ingest import collect_candidates
from jobmedley_scout.state import send_repo

#: **1件。設定では変えられない。** 1通目は1通である。
FIRST_SEND_CAP = 1

#: 成功ステータスが未確定のあいだ使う暫定の集合。**報告に必ず出す。**
PROVISIONAL_SUCCESS = frozenset(range(200, 300))

#: 保存する件名の作り方。**媒体へは届かない札である。**
SUBJECT_PREFIX = "スカウト"


class FirstSendStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    DRY_RUN_ON = "dry_run_on"
    NOT_ACKNOWLEDGED = "not_acknowledged"
    NO_CANDIDATE = "no_candidate"
    NO_MESSAGE = "no_message"
    NO_SEARCH_UUID = "no_search_uuid"
    FAILED = "failed"
    SENT = "sent"


@dataclass
class FirstSendReport:
    """What actually happened. **送ったかどうかを曖昧にしない。**"""

    dry_run: bool = True
    acknowledged: bool = False
    rows_seen: int = 0
    message: GeneratedMessage | None = None
    search_uuid: str | None = None
    result: SendResult | None = None
    #: 予約した冪等キーの有無。**送信の直前に必ずディスクへ載る** (9.2)。
    reserved: bool = False

    def reached(self) -> FirstSendStage:
        """The single stage the run actually reached.

        単調性が破れる状態は嘘なので、報告せず例外にする。
        """
        # **門は順番ではなく独立である。** 単調性の連鎖に混ぜてはいけない。
        #
        # 最初はこの2つを連鎖に入れていて、「dry_run は有効だが承知の印はある」
        # という **正常な状態** を矛盾として例外にしていた。検査が捕まえた。
        # 承知の印は入力であって、進んだ証拠ではない。
        if self.dry_run:
            return FirstSendStage.DRY_RUN_ON
        if not self.acknowledged:
            return FirstSendStage.NOT_ACKNOWLEDGED

        # ここから先は **進んだ証拠** の連鎖である。後の段の条件は前の段に
        # 含まれていなければならない。
        chain: tuple[tuple[FirstSendStage, bool], ...] = (
            (FirstSendStage.NO_CANDIDATE, self.rows_seen > 0),
            (FirstSendStage.NO_MESSAGE, self.message is not None and self.message.sendable),
            (FirstSendStage.NO_SEARCH_UUID, bool(self.search_uuid)),
            (FirstSendStage.FAILED, self.result is not None and self.result.succeeded),
        )
        stopped: FirstSendStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                raise ValueError(
                    f"FirstSendReport の状態が時系列と矛盾しています: {stopped.value} で"
                    f"止まったのに {stage.value} を通過した証拠がある"
                    " (報告を嘘にしないため停止)。"
                )
        return stopped or FirstSendStage.SENT

    def render(self) -> str:
        stage = self.reached()
        lines = ["段階4-3: 1通目の送信", ""]

        if stage is FirstSendStage.DRY_RUN_ON:
            lines.append("  **送っていません。** dry_run が有効です。")
            lines.append("  送るには SCOUT_DRY_RUN=false を明示してください (13.6)。")
            return "\n".join(lines)
        if stage is FirstSendStage.NOT_ACKNOWLEDGED:
            lines.append("  **送っていません。** 取り消せないことの確認がありません。")
            lines.append("  --i-understand-sends-are-irreversible を付けてください。")
            return "\n".join(lines)
        if stage is FirstSendStage.NO_CANDIDATE:
            lines.append("  **送っていません。** 送る相手が取れませんでした。")
            lines.append("  取り込みの報告を見てください (0件なのか、届いていないのか)。")
            return "\n".join(lines)
        if stage is FirstSendStage.NO_MESSAGE:
            lines.append("  **送っていません。** 送れる文面ができませんでした。")
            if self.message is not None:
                lines.append(f"    生成の結果: {self.message.outcome.value}")
            return "\n".join(lines)
        if stage is FirstSendStage.NO_SEARCH_UUID:
            # **黙って送らない。** 記法が残ったまま送れば媒体へそのまま渡る。
            lines.append("  **送っていません。** 検索識別子 (searchUuid) が取れませんでした。")
            lines.append("  送信payloadに要る値です。一覧の応答から持ち出せていません。")
            return "\n".join(lines)

        assert self.result is not None  # noqa: S101 -- reached() が保証している
        lines.append(f"  冪等キー: {'予約しました' if self.reserved else '**予約できていません**'}")
        lines.append(f"  HTTPステータス: {self.result.http_status}")
        if stage is FirstSendStage.FAILED:
            lines.append("  **送信は失敗しました。**")
            lines.append(f"    理由: {self.result.failure_reason or '(記録がありません)'}")
            lines.append("  記録は failed です。次回は新しい冪等キーで送り直せます (9.2)。")
            return "\n".join(lines)

        lines.append("  **送信しました。1通です。**")
        lines.extend(self._coordinate_lines())
        lines.append("")
        lines.append("  **件名は媒体へ届いていません。** 送信payloadに件名の欄がありません。")
        lines.append("  保存した件名は手元の札で、返信突合が成立するかは未確認です (10.2)。")
        return "\n".join(lines)

    def _coordinate_lines(self) -> list[str]:
        """What this one send taught us. **1通目の目的の半分はこれである。**"""
        assert self.result is not None  # noqa: S101
        return [
            "",
            "config/site_coordinates.yaml に書き残してください:",
            "",
            f"  api.send.paid.success_statuses: [{self.result.http_status}]",
            "    ← この1通が成功したときのステータスです。",
            "    (この実行では暫定で 2xx を使いました -- 未確定だったため)",
            "",
            "  # errorMessage 欄は空でした (成功時の形)。失敗時に何が入るかは、",
            "  # 実際に失敗するまで分かりません。**文言は座標に書かないこと** --",
            "  # 候補者名が混ざりうるためです (13.2)。",
        ]


def send_first(
    client: JobMedleyApiClient,
    endpoints: Mapping[str, Endpoint],
    coordinates: SiteCoordinates,
    ingest_config: IngestConfig,
    safety: SafetyConfig,
    connection: sqlite3.Connection,
    clock: Clock,
    *,
    llm: AnthropicLike,
    llm_config: LlmConfig,
    prompt_template: str,
    clinic: Mapping[str, str],
    clinic_address: str,
    max_requests: int,
    acknowledged: bool,
    run_id: str,
    destination: Path,
) -> FirstSendReport:
    """Send exactly one message. **門を通らなければ何も起きない。**"""
    report = FirstSendReport(dry_run=safety.dry_run, acknowledged=acknowledged)
    if safety.dry_run or not acknowledged:
        return report

    ingest_report, candidates = collect_candidates(
        client, endpoints, coordinates, ingest_config, safety, cap=FIRST_SEND_CAP
    )
    report.rows_seen = len(candidates)
    report.search_uuid = ingest_report.search_uuid
    if not candidates:
        return report

    candidate = candidates[0]
    report.message = generate_scout_body(
        llm,
        config=llm_config,
        prompt=build_prompt(prompt_template, clinic, candidate),
        candidate=candidate,
        clinic_address=clinic_address,
        max_requests=max_requests,
    )
    if not report.message.sendable:
        return report
    _write_body(destination, report.message.body, candidate)
    if not report.search_uuid:
        # **記法が残ったまま送らない。** assert_fully_filled も止めるが、
        # そこまで行く前に理由を名前で報告する (原則2)。
        return report

    subject = _local_subject(candidate, clock)
    reserved = send_repo.reserve_send(
        connection,
        candidate_id=candidate.candidate_id,
        message_kind=MessageKind.FIRST_CONTACT,
        followup_seq=0,
        slot=SendSlot.PAID,
        endpoint_id=SEND_PAID,
        subject=subject,
        subject_norm=subject,
        subject_prefix35=subject[:35],
        body_digest=str(len(report.message.body)),
        run_id=run_id,
        provenance="send-first",
        clock=clock,
    )
    report.reserved = True

    # **成功集合を暫定で差し替える。** 未確定の座標を要求すると梯子が閉じる。
    endpoint = _with_provisional_success(endpoints[SEND_PAID])
    result = send_message(
        client,
        endpoint,
        reserved,
        AssembledMessage(
            subject=subject,
            body=report.message.body,
            subject_norm=subject,
            subject_prefix35=subject[:35],
        ),
        payload_template=coordinates.json_path("api.send.paid.payload_template"),
        extra={PLACEHOLDER_SEARCH_UUID: report.search_uuid},
    )
    report.result = result

    if result.succeeded:
        send_repo.mark_sent(connection, reserved, result, clock)
    else:
        send_repo.mark_failed(
            connection,
            reserved,
            result.failure_reason or "理由が記録されていません",
            clock,
            http_status=result.http_status,
        )
    return report


def _with_provisional_success(endpoint: Endpoint) -> Endpoint:
    """The send endpoint with a provisional 2xx success set. **報告に出すこと。**

    ``api.send.paid.success_statuses`` は「1通送らないと分からない」座標である
    (docs/ladder.md 4-3)。送る前に要求すると梯子が閉じるので、この1通だけ暫定を
    使う。**判定の本体は ``errors`` と ``errorMessage``** で、そちらは座標を
    要求しない (api/success.py の3本立て)。
    """
    return Endpoint(
        id=endpoint.id,
        method=endpoint.method,
        url_pattern=endpoint.url_pattern,
        success_statuses=PROVISIONAL_SUCCESS,
        slot=endpoint.slot,
        side_effectful=endpoint.side_effectful,
    )


def _local_subject(candidate: Candidate, clock: Clock) -> str:
    """A label for our own records. **媒体へは届かない。**

    送信payloadに件名の欄が無いので、これは手元の記録と返信突合のための札である。
    ``reserve_send`` は空の件名を拒む (13.3: 復元不能な突合キー) ので、必ず作る。
    """
    who = candidate.member_code or candidate.candidate_id
    return f"{SUBJECT_PREFIX} {who} {clock.now().date().isoformat()}"


def _write_body(destination: Path, body: str, candidate: Candidate) -> None:
    """Keep a copy of what was sent. **送ったものは取り消せない。残す。**"""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "\n".join(
                (
                    "# 1通目に送った文面",
                    "",
                    f"- 候補者の会員番号: {candidate.member_code}",
                    f"- 長さ: {len(body)} 字 (改行込み)",
                    "",
                    "---",
                    "",
                    body,
                    "",
                )
            ),
            encoding="utf-8",
        )
    except OSError:
        return


__all__ = ["FIRST_SEND_CAP", "FirstSendReport", "FirstSendStage", "send_first"]
