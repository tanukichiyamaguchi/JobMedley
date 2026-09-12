"""段階5 の通し。**合格条件が2つあることが、下見との違いである。**

ラダー:

> - 生成文面を目視して **虚偽がない**
> - **対象外になった候補者の理由が妥当**

下見 (``preview``) は1人だけを見るので、外された人はそもそも出てこない。ここは
複数人を通すので、**通った人と外された人の両方** が出る。外した理由が妥当かどうか
は、理由が報告に出ていなければ判断しようがない。

``dryrun`` は **名前だけあって中身が無かった。** CLI に ``sub.add_parser("dryrun")``
と ``--limit`` は在ったが、本体は ``NotImplementedError`` を投げていた。座標の集合
まで登録されており、しかも **その集合が実態と合っていなかった** (取り込みの座標を
1つも要求せず、一度も呼ばない送信URLを要求していた)。

**実装されていないコマンドの前提は、検査されないまま腐る。** 誰も走らせないので
誰も気付かない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jobmedley_scout.api.endpoints import SEND_PAID, Endpoint
from jobmedley_scout.generation.scout_message import GeneratedMessage, GenerationOutcome
from jobmedley_scout.models.candidate import Candidate, ScoutHistoryEntry, ScoutHistorySummary
from jobmedley_scout.models.send_record import SendSlot
from jobmedley_scout.runtime.commands import dryrun as dryrun_module
from jobmedley_scout.runtime.commands.dryrun import (
    DRYRUN_MAX_LIMIT,
    DryRunStage,
    dryrun,
)
from jobmedley_scout.runtime.commands.ingest import IngestReport

NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)

#: 実測した送信 payload の形。**組み立てまで通すのが段階5 の要点である。**
PAYLOAD_TEMPLATE = (
    '{"operationName": "SendSingleScout",'
    ' "query": "mutation SendSingleScout($input: SendSingleScoutInput!)'
    ' { sendSingleScout(input: $input) { scoutedMemberId errorMessage __typename } }",'
    ' "variables": {"input": {'
    '"jobOfferId": 1, "jobOfferSalaryId": 2,'
    ' "memberId": "{{CANDIDATE_ID}}",'
    ' "scoutMessage": "{{BODY}}",'
    ' "searchUuid": "{{SEARCH_UUID}}"}}}'
)


class _Coordinates:
    """Only what the assembly actually asks for."""

    def __init__(self, template: str | None = PAYLOAD_TEMPLATE) -> None:
        self._template = template

    def json_path(self, key: str) -> str | None:
        assert key == "api.send.paid.payload_template"
        return self._template


def _endpoints(url: str | None = "https://example.invalid/graphql/SendSingleScout"):  # type: ignore[no-untyped-def]
    return {
        SEND_PAID: Endpoint(
            id=SEND_PAID,
            method="POST",
            url_pattern=url,
            success_statuses=None,
            slot=SendSlot.PAID,
            side_effectful=True,
        )
    }


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Llm:
    """Counts how many times the model was asked to write."""

    def __init__(self) -> None:
        self.calls = 0


def _candidate(
    index: int,
    *,
    history: ScoutHistorySummary | None = None,
    member_code: str | None = None,
    residence: str | None = "東京都中野区",
) -> Candidate:
    return Candidate(
        candidate_id=str(1000 + index),
        raw_id_observed=str(1000 + index),
        member_code=member_code if member_code is not None else f"0000{index}",
        residence=residence,
        scout_history=history,
    )


def _message(body: str = "本文" * 200) -> GeneratedMessage:
    return GeneratedMessage(
        candidate_id="1",
        outcome=GenerationOutcome.GENERATED,
        body=body,
        attempts=1,
        requests=1,
    )


@pytest.fixture()
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Run the whole command with ingest and generation replaced.

    差し替えるのは **外へ出る2つ** だけである。判定も報告も本物が走る。
    """
    llm = _Llm()

    def _go(
        candidates: list[Candidate],
        *,
        limit: int = 5,
        days: int = 3,
        endpoints: Any = None,
        coordinates: Any = None,
        search_uuid: str | None = "uuid",
    ) -> Any:
        def _collect(*_a: Any, **_k: Any) -> tuple[IngestReport, list[Candidate]]:
            report = IngestReport()
            report.search_uuid = search_uuid
            return report, candidates

        def _generate(*_a: Any, **_k: Any) -> GeneratedMessage:
            llm.calls += 1
            return _message()

        monkeypatch.setattr(dryrun_module, "collect_candidates", _collect)
        monkeypatch.setattr(dryrun_module, "generate_scout_body", _generate)
        return dryrun(
            object(),  # type: ignore[arg-type]
            _endpoints() if endpoints is None else endpoints,
            _Coordinates() if coordinates is None else coordinates,
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            _Clock(),  # type: ignore[arg-type]
            llm=object(),  # type: ignore[arg-type]
            llm_config=object(),  # type: ignore[arg-type]
            prompt_template="",
            clinic={},
            clinic_address="",
            max_requests=6,
            limit=limit,
            skip_if_scouted_within_days=days,
            destination=tmp_path / "dryrun.md",
        )

    _go.llm = llm  # type: ignore[attr-defined]
    _go.dest = tmp_path / "dryrun.md"  # type: ignore[attr-defined]
    return _go


def _observed(*sent_at: str) -> ScoutHistorySummary:
    return ScoutHistorySummary(
        entries=tuple(ScoutHistoryEntry(latest_sent_at=v, sent_count=1) for v in sent_at)
    )


# --------------------------------------------------------------------------
# 送らない
# --------------------------------------------------------------------------


def test_the_command_cannot_send_because_it_has_no_send_path(run: Any) -> None:
    """**構造で送れない。** 安全弁の値に依存しない。

    ``dryrun`` は送信エンドポイントを受け取らないので、``SCOUT_DRY_RUN`` が
    false でも1通も送れない。``preview`` と同じ形である。
    """
    import inspect

    signature = inspect.signature(dryrun)
    assert "connection" not in signature.parameters, "状態DBを受け取っている"
    source = inspect.getsource(dryrun_module)
    assert "send_message" not in source, "送信関数を呼んでいる"
    assert "send_repo" not in source, "送信記録に触れている"


def test_nothing_is_written_to_the_state_database(run: Any) -> None:
    """**保存しない。** 送っていないのに「取り込み済み」にしない。"""
    import inspect

    source = inspect.getsource(dryrun_module)
    assert "upsert_candidate" not in source


# --------------------------------------------------------------------------
# 外した理由 -- 段階5 の合格条件その2
# --------------------------------------------------------------------------


def test_the_report_separates_recent_sends_from_undeterminable(run: Any) -> None:
    """**外した理由を種別ごとに出す。**

    「直近に送った」と「判定できなかった」はまったく別の事態である。前者が
    積み上がるのは正常、後者が積み上がるのは履歴が読めていない合図である。
    数だけでは区別が付かない。
    """
    report = run(
        [
            _candidate(1, history=_observed("2026-09-11T10:00:00+00:00")),  # 直近
            _candidate(2, history=None),  # 未観測
            _candidate(3, history=ScoutHistorySummary()),  # 初回
        ]
    )
    said = report.render()
    assert "直近に送信済み: 1 名" in said
    assert "判定できず送らない側へ倒した**: 1 名" in said
    assert "通過 1 名 / 外した 2 名" in said


def test_the_reason_text_appears_not_just_the_count(run: Any) -> None:
    """件数だけでは次の手が決まらない。**根拠の文言も出す** (原則2)。"""
    report = run([_candidate(1, history=None)])
    assert "理由: 媒体のスカウト履歴を観測していません" in report.render()


def test_a_skipped_candidate_costs_no_model_call(run: Any) -> None:
    """**外した人にLLMを使わない。** 送らない相手の文面を書く理由が無い。"""
    run([_candidate(i, history=None) for i in range(1, 4)])
    assert run.llm.calls == 0


# --------------------------------------------------------------------------
# 三値と書式の観測
# --------------------------------------------------------------------------


def test_the_report_counts_how_many_histories_were_observed(run: Any) -> None:
    """観測できた人数を出す。**0 のまま通過0名なら、履歴が読めていない。**"""
    report = run([_candidate(1, history=ScoutHistorySummary()), _candidate(2, history=None)])
    assert "媒体のスカウト履歴: 1 / 2 名 観測できました" in report.render()


def test_the_report_names_the_timestamp_shape_without_the_value(run: Any) -> None:
    """**書式は出すが値は出さない** (13.2)。

    ``latestSentAt`` の書式は ``<string>`` としか観測されていない。1回走らせれば
    正体が分かるように、読めた形だけを報告に出す。
    """
    report = run([_candidate(1, history=_observed("2026-01-01T10:00:00+00:00"))])
    said = report.render()
    assert "日時 (時刻あり)" in said
    assert "2026-01-01" not in said, "日時の値がログに出ている"


# --------------------------------------------------------------------------
# 13.2 -- ログに個人データを出さない
# --------------------------------------------------------------------------


def test_the_log_never_carries_a_member_code_or_a_body(run: Any) -> None:
    """**本文も会員番号も1文字も出さない。** 出口は成果物だけである。"""
    report = run([_candidate(1, history=ScoutHistorySummary(), member_code="01613058")])
    said = report.render()
    assert "01613058" not in said
    assert "本文本文" not in said
    assert "東京都中野区" not in said


def test_the_artifact_carries_what_the_operator_must_judge(run: Any) -> None:
    """**成果物には出す。** 運用者は本文と理由を読まなければ判断できない。"""
    report = run(
        [
            _candidate(1, history=ScoutHistorySummary(), member_code="01613058"),
            _candidate(2, history=_observed("2026-09-11T10:00:00+00:00"), member_code="09999999"),
        ]
    )
    assert report.written_to is not None
    written = report.written_to.read_text(encoding="utf-8")
    assert "01613058" in written
    assert "本文" in written
    # 外された人も成果物に出る -- 理由が妥当かを判断するため。
    assert "09999999" in written
    assert "外したので文面は作っていません" in written


# --------------------------------------------------------------------------
# 段
# --------------------------------------------------------------------------


def test_no_rows_is_its_own_stage(run: Any) -> None:
    report = run([])
    assert report.reached() is DryRunStage.NO_ROWS
    assert "1件も取れませんでした" in report.render()


def test_everyone_skipped_is_not_reported_as_success(run: Any) -> None:
    """**静かなゼロ件にしない。**

    「対象が居なかった」と「全員が判定不能で外れた」はまったく別の事態である。
    """
    report = run([_candidate(i, history=None) for i in range(1, 3)])
    assert report.reached() is DryRunStage.ALL_SKIPPED
    assert "全員が直近送信の判定で外れました" in report.render()


def test_a_clean_run_reaches_ready(run: Any) -> None:
    report = run([_candidate(1, history=ScoutHistorySummary())])
    assert report.reached() is DryRunStage.READY
    assert "送信は1件も行っていません" in report.render()


# --------------------------------------------------------------------------
# 上限
# --------------------------------------------------------------------------


def test_the_limit_is_capped_so_input_cannot_run_up_the_bill(run: Any) -> None:
    """**入力で青天井にできない** (13.1)。1人ごとにLLMを呼ぶ。"""
    report = run([_candidate(1, history=ScoutHistorySummary())], limit=1000)
    assert report.limit == DRYRUN_MAX_LIMIT


def test_a_zero_or_negative_limit_still_runs_one(run: Any) -> None:
    """0 や負の値で「黙って0件」にしない。最低1名は通す。"""
    report = run([_candidate(1, history=ScoutHistorySummary())], limit=0)
    assert report.limit == 1


# --------------------------------------------------------------------------
# 組み立て -- 「送信直前で止める」の「直前」
# --------------------------------------------------------------------------


def test_the_send_is_assembled_but_never_called(run: Any) -> None:
    """**止まる直前まで組み立てる。**

    一度この要求を座標から外そうとして、ガードレール
    (``test_dryrun_still_requires_what_it_needs_to_build_a_send``) に止められた。
    検査の言い分が正しい:

    > 「送信直前で止める」は、止まる直前まで組み立てるということである。
    > 組み立てられない状態で「空振り成功」と報告したら、それは原則2 の
    > 「静かなゼロ件」を手順書の側で作ることになる。
    """
    report = run([_candidate(1, history=ScoutHistorySummary())])
    assert report.ready, "組み立てまで通っていない"
    assert "組み立て: 1 / 1 件 通りました (**呼んでいません**)" in report.render()


def test_a_broken_template_is_not_reported_as_a_clean_rehearsal(run: Any) -> None:
    """雛形が壊れていたら **合格にしない。**

    段階5 が通って段階6 が組み立てで落ちるなら、予行演習が予行になっていない。
    """
    report = run(
        [_candidate(1, history=ScoutHistorySummary())],
        coordinates=_Coordinates('{"operationName": "SendSingleScout"}'),  # query が無い
    )
    assert report.reached() is DryRunStage.NOTHING_ASSEMBLED
    said = report.render()
    assert "組み立てられませんでした" in said
    assert "段階6へ進むと、送信の直前で落ちます" in said


def test_a_missing_search_uuid_stops_the_assembly(run: Any) -> None:
    """**記法が残ったまま組み立てたことにしない。**

    ``searchUuid`` は実行時にしか分からない値で、埋まっていなければ媒体には
    送れない。ここで通してしまうと、段階6 の1通目で初めて分かる。
    """
    report = run([_candidate(1, history=ScoutHistorySummary())], search_uuid=None)
    assert report.reached() is DryRunStage.NOTHING_ASSEMBLED
    assert "検索識別子 (searchUuid) が取れていません" in report.render()


def test_the_assembly_failure_never_leaks_a_value(run: Any) -> None:
    """13.2: 失敗の理由に **値を含めない。**"""
    report = run(
        [_candidate(1, history=ScoutHistorySummary(), member_code="01613058")],
        coordinates=_Coordinates('{"operationName": "SendSingleScout"}'),
    )
    said = report.render()
    assert "01613058" not in said
    assert "本文本文" not in said
