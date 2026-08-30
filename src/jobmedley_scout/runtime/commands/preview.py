"""1通目の文面を **下見する**。送信は1件も行わない。

段階4-3 は「1通だけ、文面を確認してから」である。確認できなければ進めない。
だが本文には確認に要るものが最初から入っている -- **会員番号での宛名**
(プロンプト STEP3 (2)) と **居住地の市区町村** (STEP1 の通勤の一文)。

13.2 は偵察の出力に個人データを残すことを禁じている。ここは偵察ではなく本番の
生成だが、置き場所が同じ Actions のログである以上、扱いは同じにする。

**だから分ける。**

* ログに出るのは **数と種別だけ** -- 通ったか、何回書き直したか、どの決まりに
  引っかかったか、何字か。**本文も会員番号も1文字も出ない**
* 本文は成果物 (artifact) に書き、運用者が落として読む。保持日数は偵察ダンプと
  同じ短さにしてある

**保存しない。** 下見は候補者を状態DBへ書かない。書くと、送っていないのに
「取り込み済み」になり、後から見て送信対象だったのかが分からなくなる。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from jobmedley_scout.api.client import JobMedleyApiClient
from jobmedley_scout.api.endpoints import Endpoint
from jobmedley_scout.config.schema import IngestConfig, LlmConfig, SafetyConfig
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.generation.llm_client import AnthropicLike
from jobmedley_scout.generation.scout_message import (
    GeneratedMessage,
    build_prompt,
    generate_scout_body,
)
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.runtime.commands.ingest import IngestReport, collect_candidates

#: 下見で取ってくる候補者の数。**1件で足りる。**
PREVIEW_CAP = 1


class PreviewStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    NO_ROWS = "no_rows"
    NO_USABLE_CANDIDATE = "no_usable_candidate"
    NOT_GENERATED = "not_generated"
    READY = "ready"


@dataclass
class PreviewReport:
    """The run, in the shape the **log** needs. 本文も会員番号も持たない。"""

    ingest: IngestReport
    rows: int = 0
    #: 生成に回せた候補者が居たか (会員番号と居住地が揃っている人)。
    usable: bool = False
    message: GeneratedMessage | None = None
    written_to: Path | None = None
    #: 生成に回せなかった理由の内訳。**人数だけを数える** (13.2)。
    skipped: dict[str, int] = field(default_factory=dict)

    def reached(self) -> PreviewStage:
        """The single stage the run actually reached. **報告はこれだけを見る。**"""
        chain: tuple[tuple[PreviewStage, bool], ...] = (
            (PreviewStage.NO_ROWS, self.rows > 0),
            (PreviewStage.NO_USABLE_CANDIDATE, self.usable),
            (PreviewStage.NOT_GENERATED, bool(self.message and self.message.sendable)),
        )
        stopped: PreviewStage | None = None
        for stage, passed in chain:
            if not passed and stopped is None:
                stopped = stage
            elif passed and stopped is not None:
                raise ValueError(
                    f"PreviewReport の状態が時系列と矛盾しています: {stopped.value} で"
                    f"止まったのに {stage.value} を通過した証拠がある。"
                    " どこかがブール値を事実と違えて立てています (報告を嘘にしないため停止)。"
                )
        return stopped or PreviewStage.READY

    def render(self) -> str:
        """**本文も会員番号も1文字も出さない** (13.2)。数と種別だけ。"""
        lines = ["段階4-3 の下見: 1通目の文面 (**送信は1件も行っていません**)", ""]
        lines.extend(self.ingest.render().splitlines())
        lines.append("")
        stage = self.reached()

        if stage is PreviewStage.NO_ROWS:
            lines.append("  **候補者を1件も取れませんでした。** 上の一覧APIの結果を見てください。")
            return "\n".join(lines)

        if self.skipped:
            lines.append("  生成に回さなかった候補者:")
            for reason, count in sorted(self.skipped.items()):
                lines.append(f"    {reason}: {count} 名")
        if stage is PreviewStage.NO_USABLE_CANDIDATE:
            lines.append("  **生成に回せる候補者が居ませんでした。**")
            lines.append("  会員番号と居住地の両方が要ります (どちらも欠けたら書かせません)。")
            return "\n".join(lines)

        message = self.message
        assert message is not None  # noqa: S101 -- reached() が保証している
        lines.append("  生成:")
        lines.append(f"    結果: {message.outcome.value}")
        lines.append(f"    書き直し: {message.attempts} 回 / API呼び出し: {message.requests} 回")
        lines.append(
            f"    トークン: in={message.usage.input_tokens} / out={message.usage.output_tokens}"
        )
        if message.used_fallback:
            # **黙らない。** 常時発火しているなら API 側の仕様が変わった合図 (8.2)。
            lines.append("    **思考オフのフォールバックが発火しました** (API仕様変更の疑い)")
        if message.failure:
            lines.append(f"    失敗: {message.failure}")
        if message.violations:
            lines.append("    残った違反 (**本文は出しません**):")
            for violation in message.violations:
                lines.append(f"      {violation.kind.value}: {violation.evidence}")
        if message.body:
            # 長さは数である。本文ではない。
            lines.append(f"    本文の長さ: {len(message.body)} 字 (改行込み)")

        if stage is PreviewStage.NOT_GENERATED:
            lines.append("")
            lines.append("  **送れる文面はできていません。** 上の違反か失敗を見てください。")
            return "\n".join(lines)

        lines.append("")
        if self.written_to is not None:
            lines.append(f"  文面を書き出しました: {self.written_to}")
            lines.append("  **ログには出していません。** 成果物を落として中身を読んでください。")
        else:
            lines.append("  **文面を書き出せませんでした。** 保存先を確認してください。")
        lines.append("")
        lines.append("次: 文面に問題が無ければ、1通だけ送る手順へ進みます。")
        lines.append("**このコマンドは送信を1件も行っていません。**")
        return "\n".join(lines)


def _reason_for(candidate: Candidate) -> str | None:
    """Why this candidate cannot be written for. ``None`` if they can.

    **理由は種別だけを返す。** 誰が欠けているかは数で足りる (13.2)。
    """
    if not (candidate.member_code or "").strip():
        return "会員番号が無い"
    if not (candidate.residence or "").strip():
        return "居住地が無い"
    return None


def preview(
    client: JobMedleyApiClient,
    endpoints: Mapping[str, Endpoint],
    coordinates: SiteCoordinates,
    ingest_config: IngestConfig,
    safety: SafetyConfig,
    *,
    llm: AnthropicLike,
    llm_config: LlmConfig,
    prompt_template: str,
    clinic: Mapping[str, str],
    clinic_address: str,
    max_requests: int,
    destination: Path,
) -> PreviewReport:
    """Ingest one candidate, write one message, and put it in an artifact.

    **保存しない。送らない。** 取ってきた候補者は状態DBへ書かないし、送信APIは
    呼ばない -- そもそもこの関数は送信エンドポイントを受け取っていない。
    """
    ingest_report, candidates = collect_candidates(
        client, endpoints, coordinates, ingest_config, safety, cap=PREVIEW_CAP
    )
    report = PreviewReport(ingest=ingest_report, rows=len(candidates))

    chosen: Candidate | None = None
    for candidate in candidates:
        reason = _reason_for(candidate)
        if reason is None:
            chosen = candidate
            break
        report.skipped[reason] = report.skipped.get(reason, 0) + 1
    if chosen is None:
        return report
    report.usable = True

    prompt = build_prompt(prompt_template, clinic, chosen)
    report.message = generate_scout_body(
        llm,
        config=llm_config,
        prompt=prompt,
        candidate=chosen,
        clinic_address=clinic_address,
        max_requests=max_requests,
    )
    if report.message.sendable:
        report.written_to = _write(destination, report.message, chosen)
    return report


def _write(destination: Path, message: GeneratedMessage, candidate: Candidate) -> Path | None:
    """Write the body where a human can read it. ``None`` if it could not be written.

    **書けなかったことを黙らない。** 黙ると、報告は「できました」と言うのに
    落とすものが無い、という食い違いになる (原則2)。
    """
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            "\n".join(
                (
                    "# スカウト文の下見 (**まだ送っていません**)",
                    "",
                    f"- 候補者の会員番号: {candidate.member_code}",
                    f"- 居住地: {candidate.residence}",
                    f"- 書き直し: {message.attempts} 回",
                    f"- 長さ: {len(message.body)} 字 (改行込み)",
                    "",
                    "---",
                    "",
                    message.body,
                    "",
                )
            ),
            encoding="utf-8",
        )
    except OSError:
        return None
    return destination


__all__ = ["PREVIEW_CAP", "PreviewReport", "PreviewStage", "preview"]
