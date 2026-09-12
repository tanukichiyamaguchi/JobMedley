"""段階5: **取り込みから生成まで通し、送信直前で止める。**

ラダーの合格条件は2つある。

> - 生成文面を目視して **虚偽がない**
> - **対象外になった候補者の理由が妥当**

2つ目が下見 (``preview``) との違いである。下見は1人だけを見るので、外された人は
そもそも出てこない。ここは複数人を通すので、**通った人と外された人の両方** が出る。
外した理由が妥当かどうかは、理由が報告に出ていなければ判断しようがない。

**送らない。** この関数は送信エンドポイントを受け取っていないので、構造的に送れ
ない。``preview`` と同じ形である -- 安全弁の値に依存せず、配線そのもので送れない
ようにしてある。

**保存しない。** 候補者を状態DBへ書かない。書くと、送っていないのに「取り込み
済み」になり、後から見て送信対象だったのかが分からなくなる (``preview`` の注記と
同じ理由)。

13.2: ログに出るのは **数と種別だけ**。本文も会員番号も1文字も出ない。本文は成果物
へ書き、運用者が落として読む。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from jobmedley_scout.api.client import JobMedleyApiClient
from jobmedley_scout.api.endpoints import SEND_PAID, Endpoint
from jobmedley_scout.api.payloads import PLACEHOLDER_SEARCH_UUID, build_send_payload
from jobmedley_scout.api.send import build_url
from jobmedley_scout.clock import Clock
from jobmedley_scout.config.placeholders import require
from jobmedley_scout.config.schema import IngestConfig, LlmConfig, SafetyConfig
from jobmedley_scout.config.site_coordinates import SiteCoordinates
from jobmedley_scout.errors import ConfigError, UnresolvedCoordinateError
from jobmedley_scout.generation.llm_client import AnthropicLike
from jobmedley_scout.generation.scout_message import (
    GeneratedMessage,
    build_prompt,
    generate_scout_body,
)
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.runtime.commands.ingest import IngestReport, collect_candidates
from jobmedley_scout.runtime.commands.send_first import local_subject
from jobmedley_scout.state.recency import describe_format, scouted_within, should_skip
from jobmedley_scout.targeting.determination import Determination, RuleOutcome

#: **人数の上限。** 1人ごとにLLMを呼ぶので、青天井にすると費用が跳ねる (13.1)。
#: 段階5 の例示が ``--limit 5`` なので、その倍を天井にしてある。
DRYRUN_MAX_LIMIT = 10


class DryRunStage(StrEnum):
    """辿り着いた段。**時系列の順に並んでいる。**"""

    NO_ROWS = "no_rows"
    ALL_SKIPPED = "all_skipped"
    NOTHING_GENERATED = "nothing_generated"
    NOTHING_ASSEMBLED = "nothing_assembled"
    READY = "ready"


@dataclass
class CandidateOutcome:
    """1人分の結果。**会員番号も本文も持たない** (13.2)。"""

    #: 直近送信の判定。外れた人も通った人も持つ。
    recency: RuleOutcome
    #: 生成の結果。外された人は ``None`` (生成まで行かない)。
    message: GeneratedMessage | None = None
    #: 生成に回せなかった理由 (会員番号や居住地が無い等)。
    unusable: str | None = None
    #: 媒体の履歴を観測できたか。**三値の観測そのものを数える。**
    history_observed: bool = False
    #: 履歴に載っていた送信日時の書式。**値は含まない** (13.2)。
    sent_at_shapes: tuple[str, ...] = ()
    #: **送信payloadを組み立てられたか。** 段階5 の要点のひとつである。
    assembled: bool = False
    #: 組み立てに失敗した理由。**値は含まない。**
    assembly_error: str | None = None

    @property
    def skipped(self) -> bool:
        return should_skip(self.recency)

    @property
    def sendable(self) -> bool:
        return self.message is not None and self.message.sendable

    @property
    def ready(self) -> bool:
        """送れる文面があり、**かつ組み立てられた**。"""
        return self.sendable and self.assembled


@dataclass
class DryRunReport:
    """The rehearsal, in the shape the **log** needs."""

    ingest: IngestReport
    limit: int = 0
    outcomes: list[CandidateOutcome] = field(default_factory=list)
    written_to: Path | None = None

    @property
    def rows(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> list[CandidateOutcome]:
        return [o for o in self.outcomes if not o.skipped]

    @property
    def sendable(self) -> list[CandidateOutcome]:
        return [o for o in self.passed if o.sendable]

    @property
    def ready(self) -> list[CandidateOutcome]:
        """**組み立てまで通った人。** 段階5 が本当に通ったのはここである。"""
        return [o for o in self.sendable if o.assembled]

    def reached(self) -> DryRunStage:
        """The single stage the run actually reached. **報告はこれだけを見る。**"""
        chain: tuple[tuple[DryRunStage, bool], ...] = (
            (DryRunStage.NO_ROWS, self.rows > 0),
            (DryRunStage.ALL_SKIPPED, bool(self.passed)),
            (DryRunStage.NOTHING_GENERATED, bool(self.sendable)),
            (DryRunStage.NOTHING_ASSEMBLED, bool(self.ready)),
        )
        stopped: DryRunStage | None = None
        for stage, ok in chain:
            if not ok and stopped is None:
                stopped = stage
            elif ok and stopped is not None:
                raise ValueError(
                    f"DryRunReport の状態が時系列と矛盾しています: {stopped.value} で"
                    f"止まったのに {stage.value} を通過した証拠がある"
                    " (報告を嘘にしないため停止)。"
                )
        return stopped or DryRunStage.READY

    def render(self) -> str:
        """**本文も会員番号も1文字も出さない** (13.2)。数と種別だけ。"""
        lines = ["段階5: 送信直前までの通し (**送信は1件も行っていません**)", ""]
        lines.extend(self.ingest.render().splitlines())
        lines.append("")

        stage = self.reached()
        if stage is DryRunStage.NO_ROWS:
            lines.append("  **候補者を1件も取れませんでした。** 上の一覧APIの結果を見てください。")
            return "\n".join(lines)

        lines.append(f"  通した候補者: {self.rows} 名 (上限 {self.limit})")
        lines.extend(self._recency_lines())
        lines.extend(self._history_lines())
        lines.extend(self._generation_lines())
        lines.extend(self._assembly_lines())

        if stage is DryRunStage.ALL_SKIPPED:
            lines.append("")
            lines.append("  **全員が直近送信の判定で外れました。** 上の内訳を見てください。")
            return "\n".join(lines)
        if stage is DryRunStage.NOTHING_GENERATED:
            lines.append("")
            lines.append("  **送れる文面が1通もできませんでした。** 上の違反を見てください。")
            return "\n".join(lines)
        if stage is DryRunStage.NOTHING_ASSEMBLED:
            lines.append("")
            lines.append(
                "  **送信payloadを1件も組み立てられませんでした。**"
                " 文面はできているので、止まっているのは組み立てです。"
            )
            lines.append("  この状態で段階6へ進むと、送信の直前で落ちます。")
            return "\n".join(lines)

        lines.append("")
        if self.written_to is not None:
            lines.append(f"  文面を書き出しました: {self.written_to}")
            lines.append("  **ログには出していません。** 成果物を落として中身を読んでください。")
        else:
            lines.append("  **文面を書き出せませんでした。** 保存先を確認してください。")
        lines.append("")
        lines.append("段階5 の合格条件は2つです。どちらも人が目で見ます。")
        lines.append("  1. 生成文面に虚偽がないか")
        lines.append("  2. 外された候補者の理由が妥当か")
        lines.append("**このコマンドは送信を1件も行っていません。**")
        return "\n".join(lines)

    def _recency_lines(self) -> list[str]:
        """外した人の内訳。**理由ごとに数える** (原則2)。"""
        skipped = [o for o in self.outcomes if o.skipped]
        lines = [f"  直近送信の判定: 通過 {len(self.passed)} 名 / 外した {len(skipped)} 名"]
        if not skipped:
            return lines
        by_reason: dict[str, int] = {}
        for outcome in skipped:
            key = (
                "直近に送信済み"
                if outcome.recency.determination is Determination.MATCH
                else "**判定できず送らない側へ倒した**"
            )
            by_reason[key] = by_reason.get(key, 0) + 1
        for reason, count in sorted(by_reason.items()):
            lines.append(f"    {reason}: {count} 名")
        seen: set[str] = set()
        for outcome in skipped:
            if outcome.recency.evidence in seen:
                continue
            seen.add(outcome.recency.evidence)
            lines.append(f"      理由: {outcome.recency.evidence}")
        return lines

    def _history_lines(self) -> list[str]:
        """観測できた履歴と、**送信日時の書式**。

        書式は 2026-08-22 の観測では ``<string>`` としか分かっていない。判定は
        寛容に読む作りだが、**読めた形を報告に出せば1回で正体が分かる。**
        """
        observed = sum(1 for o in self.outcomes if o.history_observed)
        lines = [f"  媒体のスカウト履歴: {observed} / {self.rows} 名 観測できました"]
        shapes: dict[str, int] = {}
        for outcome in self.outcomes:
            for shape in outcome.sent_at_shapes:
                shapes[shape] = shapes.get(shape, 0) + 1
        if shapes:
            lines.append("    送信日時の書式 (**値は出していません**):")
            for shape, count in sorted(shapes.items()):
                lines.append(f"      {shape}: {count} 件")
        return lines

    def _assembly_lines(self) -> list[str]:
        """組み立ての結果。**段階5 が本当に通ったかはここで決まる。**

        「送信直前で止める」は、止まる直前まで組み立てるということである。
        組み立てられない状態で「空振り成功」と報告したら、段階5 が通って段階6 が
        組み立てで落ちる -- 予行演習が予行になっていない。
        """
        if not self.sendable:
            return []
        lines = [
            f"  送信payloadの組み立て: {len(self.ready)} / {len(self.sendable)} 件"
            " 通りました (**呼んでいません**)"
        ]
        errors: dict[str, int] = {}
        for outcome in self.sendable:
            if outcome.assembly_error:
                errors[outcome.assembly_error] = errors.get(outcome.assembly_error, 0) + 1
        for reason, count in sorted(errors.items()):
            lines.append(f"    **組み立てられませんでした**: {reason} ({count} 件)")
        return lines

    def _generation_lines(self) -> list[str]:
        """生成の結果。**本文は出さない。**"""
        lines = [f"  生成: {len(self.sendable)} / {len(self.passed)} 通 送れる文面になりました"]
        unusable: dict[str, int] = {}
        for outcome in self.passed:
            if outcome.unusable:
                unusable[outcome.unusable] = unusable.get(outcome.unusable, 0) + 1
        for reason, count in sorted(unusable.items()):
            lines.append(f"    生成に回せなかった: {reason} ({count} 名)")

        violations: dict[str, int] = {}
        fallbacks = 0
        for outcome in self.passed:
            message = outcome.message
            if message is None:
                continue
            if message.used_fallback:
                fallbacks += 1
            for violation in message.violations:
                violations[violation.kind.value] = violations.get(violation.kind.value, 0) + 1
        if violations:
            lines.append("    残った違反 (**本文は出しません**):")
            for kind, count in sorted(violations.items()):
                lines.append(f"      {kind}: {count} 件")
        if fallbacks:
            # **黙らない。** 常時発火しているなら API 側の仕様変更の合図 (8.2)。
            lines.append(f"    **思考オフのフォールバックが {fallbacks} 回発火しました**")
        return lines


def dryrun(
    client: JobMedleyApiClient,
    endpoints: Mapping[str, Endpoint],
    coordinates: SiteCoordinates,
    ingest_config: IngestConfig,
    safety: SafetyConfig,
    clock: Clock,
    *,
    llm: AnthropicLike,
    llm_config: LlmConfig,
    prompt_template: str,
    clinic: Mapping[str, str],
    clinic_address: str,
    max_requests: int,
    limit: int,
    skip_if_scouted_within_days: int,
    destination: Path,
) -> DryRunReport:
    """Run the whole path up to the send, and stop. **送信路を持たない。**"""
    capped = max(1, min(limit, DRYRUN_MAX_LIMIT))
    ingest_report, candidates = collect_candidates(
        client, endpoints, coordinates, ingest_config, safety, cap=capped
    )
    report = DryRunReport(ingest=ingest_report, limit=capped)

    now = clock.now()
    for candidate in candidates:
        outcome = CandidateOutcome(
            recency=scouted_within(
                candidate.scout_history, now=now, days=skip_if_scouted_within_days
            ),
            history_observed=candidate.scout_history is not None,
            sent_at_shapes=_sent_at_shapes(candidate),
        )
        report.outcomes.append(outcome)
        if outcome.skipped:
            # **外した人にLLMを使わない。** 送らない相手の文面を書く理由が無い。
            continue
        reason = _unusable_reason(candidate)
        if reason is not None:
            outcome.unusable = reason
            continue
        outcome.message = generate_scout_body(
            llm,
            config=llm_config,
            prompt=build_prompt(prompt_template, clinic, candidate),
            candidate=candidate,
            clinic_address=clinic_address,
            max_requests=max_requests,
        )
        if not outcome.sendable:
            continue
        # **止まる直前まで組み立てる。** 「送信直前で止める」とはそういう意味で
        # ある。組み立てられない状態で「空振り成功」と報告すれば、段階5 が通って
        # 段階6 が組み立てで落ちる -- 予行演習が予行になっていない (原則2)。
        #
        # **呼ばない。** 組み立てた payload も URL も捨てる。この関数はクライアント
        # を送信に使わないので、組み立てた結果が媒体へ出ていく経路が無い。
        outcome.assembled, outcome.assembly_error = _try_assemble(
            endpoints,
            coordinates,
            candidate,
            body=outcome.message.body,
            subject=local_subject(candidate, clock),
            search_uuid=ingest_report.search_uuid,
        )

    if report.ready:
        report.written_to = _write(destination, candidates, report)
    return report


def _try_assemble(
    endpoints: Mapping[str, Endpoint],
    coordinates: SiteCoordinates,
    candidate: Candidate,
    *,
    body: str,
    subject: str,
    search_uuid: str | None,
) -> tuple[bool, str | None]:
    """Build the send URL and payload, then throw them away.

    返すのは **組み立てられたかどうかと、駄目だった理由の種別だけ** である。
    組み立てたものは返さない -- 返せば、呼び出し側がうっかり送れる経路になる。

    **理由に値を含めない** (13.2)。例外の文言には記法や欄の名前しか出ないが、
    念のため型と要点だけを取り出す。
    """
    if not search_uuid:
        # **記法が残ったまま組み立てたことにしない。** searchUuid は実行時にしか
        # 分からない値で、埋まっていなければ媒体には送れない。
        return False, "検索識別子 (searchUuid) が取れていません"
    endpoint = endpoints.get(SEND_PAID)
    if endpoint is None:
        return False, "送信エンドポイントが登録されていません"
    try:
        pattern = require(endpoint.url_pattern, used_by="runtime.commands.dryrun")
        if pattern is None:
            return False, "送信URLが null です (この枠は存在しません)"
        build_url(pattern, candidate.candidate_id)
        build_send_payload(
            coordinates.json_path("api.send.paid.payload_template"),
            candidate_id=candidate.candidate_id,
            subject=subject,
            body=body,
            followup_days=None,
            used_by="runtime.commands.dryrun",
            extra={PLACEHOLDER_SEARCH_UUID: search_uuid},
        )
    except UnresolvedCoordinateError:
        return False, "送信の座標が未確定です"
    except ConfigError as exc:
        return False, f"組み立てに失敗しました ({type(exc).__name__})"
    return True, None


def _sent_at_shapes(candidate: Candidate) -> tuple[str, ...]:
    """The shapes of this candidate's send timestamps. **値は含まない** (13.2)。"""
    history = candidate.scout_history
    if history is None:
        return ()
    return tuple(describe_format(entry.latest_sent_at) for entry in history.entries)


def _unusable_reason(candidate: Candidate) -> str | None:
    """Why this candidate cannot be written for. ``None`` if they can.

    ``preview`` の ``_reason_for`` と同じ判定である。**種別だけを返す** (13.2)。
    """
    if not (candidate.member_code or "").strip():
        return "会員番号が無い"
    if not (candidate.residence or "").strip():
        return "居住地が無い"
    return None


def _write(destination: Path, candidates: Sequence[Candidate], report: DryRunReport) -> Path | None:
    """Write every generated body where a human can read it.

    **書けなかったことを黙らない。** 黙ると、報告は「できました」と言うのに落とす
    ものが無い、という食い違いになる (原則2)。
    """
    blocks: list[str] = [
        "# 段階5 の通し (**1通も送っていません**)",
        "",
        f"- 通した候補者: {report.rows} 名",
        f"- 直近送信の判定で外した: {report.rows - len(report.passed)} 名",
        f"- 送れる文面になった: {len(report.sendable)} 通",
        f"- 送信payloadまで組み立てられた: {len(report.ready)} 通 (**送っていません**)",
        "",
        "合格条件は2つです。**どちらも人が目で見ます。**",
        "",
        "1. 生成文面に虚偽がないか",
        "2. 外された候補者の理由が妥当か",
        "",
    ]
    for index, (candidate, outcome) in enumerate(zip(candidates, report.outcomes, strict=False), 1):
        blocks.append("---")
        blocks.append("")
        blocks.append(f"## {index}. 会員番号 {candidate.member_code or '(無し)'}")
        blocks.append("")
        blocks.append(f"- 居住地: {candidate.residence or '(無し)'}")
        blocks.append(f"- 直近送信の判定: {outcome.recency.determination.value}")
        blocks.append(f"  - {outcome.recency.evidence}")
        history = candidate.scout_history
        if history is None:
            blocks.append("- 媒体の履歴: **観測できていません**")
        else:
            blocks.append(
                f"- 媒体の履歴: 観測済み / 自社からの送信 {len(history.entries)} 件"
                f" (通算 {history.total_sent() if history.total_sent() is not None else '不明'})"
            )
        if outcome.skipped:
            blocks.append("")
            blocks.append("**外したので文面は作っていません。**")
            blocks.append("")
            continue
        if outcome.unusable:
            blocks.append("")
            blocks.append(f"**生成に回していません: {outcome.unusable}**")
            blocks.append("")
            continue
        message = outcome.message
        if message is None or not message.body:
            blocks.append("")
            blocks.append("**文面ができませんでした。**")
            blocks.append("")
            continue
        blocks.append(f"- 書き直し: {message.attempts} 回 / 長さ: {len(message.body)} 字")
        if message.violations:
            blocks.append("- 残った違反:")
            blocks.extend(f"  - {v.kind.value}: {v.evidence}" for v in message.violations)
        blocks.append("")
        blocks.append(message.body)
        blocks.append("")

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(blocks), encoding="utf-8")
    except OSError:
        return None
    return destination


__all__ = ["DRYRUN_MAX_LIMIT", "CandidateOutcome", "DryRunReport", "DryRunStage", "dryrun"]
