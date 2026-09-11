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
from jobmedley_scout.config.effective import SOURCE_CONFIG
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
from jobmedley_scout.runtime.commands.ingest import SOURCE, collect_candidates
from jobmedley_scout.state import candidate_repo, send_repo

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
    #: dry_run の値が **どこから来たか** (12.6)。値だけでは配線漏れが隠れる。
    dry_run_source: str = ""
    acknowledged: bool = False
    rows_seen: int = 0
    message: GeneratedMessage | None = None
    search_uuid: str | None = None
    result: SendResult | None = None
    #: 送る相手を状態DBへ保存したか。**予約の前提である** (外部キー)。
    stored: bool = False
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
            # **由来を必ず添える** (12.6)。実測48回目に、ワークフローは
            # SCOUT_DRY_RUN=false を渡しているのにここで止まった。由来が出て
            # いれば「config.yaml から来ている」= 環境変数が届いていない、が
            # その場で分かった。**値だけの報告は、届いていない配線を隠す。**
            lines.append(f"  dry_run の由来: {self.dry_run_source or '不明'}")
            if self.dry_run_source == SOURCE_CONFIG:
                lines.append(
                    "  **環境変数が届いていません。** SCOUT_DRY_RUN を渡したのに"
                    " config.yaml の値が使われています (12.6 の配線漏れ)。"
                )
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
        lines.append(f"  送る相手の保存: {'しました' if self.stored else '**していません**'}")
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
    dry_run_source: str = "",
) -> FirstSendReport:
    """Send exactly one message. **門を通らなければ何も起きない。**"""
    report = FirstSendReport(
        dry_run=safety.dry_run,
        dry_run_source=dry_run_source,
        acknowledged=acknowledged,
    )
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
    _write_body(destination, report.message.body, candidate, BEFORE_SEND)
    if not report.search_uuid:
        # **記法が残ったまま送らない。** assert_fully_filled も止めるが、
        # そこまで行く前に理由を名前で報告する (原則2)。
        return report

    # **送る相手を先に保存する。** ``send_records.candidate_id`` は
    # ``candidates(candidate_id)`` への外部キーなので、保存していない相手には
    # 冪等キーを予約できない。
    #
    # ここは :func:`collect_candidates` を下見と共用している。下見は **保存しない
    # ことが要点** で (送っていないのに「取り込み済み」になるため)、その関数を
    # そのまま送信路で使ったので保存が抜けた。2026-09-11、実送信の1通目が
    # ``FOREIGN KEY constraint failed`` で落ちて分かった。
    #
    # **送信路では保存しないという選択肢が無い。** 誰に送ったか分からない送信は
    # 9.4 が禁じている (送信枠は後から復元できない) し、次回の重複判定もここを
    # 見る (9.3)。下見と送信で保存の要否が正反対である、というのが要点だった。
    candidate_repo.upsert_candidate(connection, candidate, source=SOURCE, clock=clock)
    report.stored = True

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

    # **見出しを結果で書き直す。** 送る前に書いたものは「送ろうとしている」と
    # 題してある。ここまで来たので、届いたかどうかが確定した。
    _write_body(
        destination,
        report.message.body,
        candidate,
        AFTER_SENT if result.succeeded else AFTER_FAILED,
    )

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


#: 送信の前に書く見出し。**まだ送っていない。**
#:
#: 送信の途中で実行が落ちると、ファイルはこの見出しのまま残る。だから
#: 「送っていません」と言い切らず、**届いたかどうかは分からない** と書く。
#: 落ちた場合に残る文面が、そのまま嘘にならないようにするためである。
BEFORE_SEND = (
    "# 1通目に送ろうとしている文面 (**この時点ではまだ送っていません**)\n"
    "\n"
    "> このファイルがこの見出しのままなら、**送信の結果が書かれる前に実行が"
    "落ちています。**\n"
    "> その場合、届いたかどうかは **このファイルからは分かりません**。"
    "ログの報告と媒体の送信履歴を見てください。"
)

#: 送信が成功したあとに書き直す見出し。
AFTER_SENT = "# 1通目に **送った** 文面 (送信は成功しました)"

#: 送信が確定失敗したあとに書き直す見出し。
AFTER_FAILED = "# 1通目に送ろうとした文面 (**送信は失敗しました。届いていません**)"


def _write_body(destination: Path, body: str, candidate: Candidate, heading: str) -> None:
    """Keep a copy of the message. **送ったものは取り消せない。残す。**

    **見出しは呼び出し側が渡す。** 以前は「1通目に送った文面」で決め打ちだった
    が、この関数は **送信の前に** 呼ばれる。つまり送る前から「送った」と書いて
    いた。``searchUuid`` が取れずに引き返した場合、送っていないのに「送った文面」
    と題されたファイルだけが成果物に残る。

    **送る前に書くこと自体は正しい。** 送信の途中で落ちても文面が残るのは、
    取り消せない操作の記録として要る。直すべきは見出しのほうである。
    """
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "\n".join(
                (
                    heading,
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
