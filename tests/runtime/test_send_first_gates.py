"""**1通目の門。** ここが取り返しのつかない唯一の場所である (13.6)。

送ったものは取り消せない。相手の受信箱に残り、月次の送信枠を1通消費する。
だから門は「開いていること」ではなく **「閉じていること」** を検査する。

門を通らなかったとき、この関数は候補者を取りに行きもしない。**LLMも呼ばない。**
呼んでいたら、門は「送信は止めたが金は使った」という半端なものになる。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobmedley_scout.config.schema import IngestConfig, SafetyConfig
from jobmedley_scout.runtime.commands.send_first import (
    FIRST_SEND_CAP,
    PROVISIONAL_SUCCESS,
    FirstSendReport,
    FirstSendStage,
    send_first,
)


class _Exploding:
    """Anything touched here means the gate leaked."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"門を通っていないのに {name} が呼ばれました")


def _safety(*, dry_run: bool) -> SafetyConfig:
    return SafetyConfig(
        dry_run=dry_run,
        state_loss_guard=True,
        kill_switch_path=Path("/tmp/kill"),
        ingest_cap=200,
        max_llm_requests_per_message=6,
    )


def _call(*, dry_run: bool, acknowledged: bool) -> FirstSendReport:
    """Call with every collaborator booby-trapped. **触れたら落ちる。**"""
    return send_first(
        _Exploding(),  # type: ignore[arg-type]
        {},
        _Exploding(),  # type: ignore[arg-type]
        IngestConfig(search_condition_id="1", page_size=25, max_pages=1, fetch_resumes=True),
        _safety(dry_run=dry_run),
        _Exploding(),  # type: ignore[arg-type]
        _Exploding(),  # type: ignore[arg-type]
        llm=_Exploding(),
        llm_config=_Exploding(),  # type: ignore[arg-type]
        prompt_template="",
        clinic={},
        clinic_address="",
        max_requests=6,
        acknowledged=acknowledged,
        run_id="test",
        destination=Path("/tmp/never-written.md"),
    )


def test_dry_run_stops_before_anything_is_touched() -> None:
    """**既定は送らない側である。** dry_run が有効なら候補者も取りに行かない。"""
    report = _call(dry_run=True, acknowledged=True)
    assert report.reached() is FirstSendStage.DRY_RUN_ON
    assert "送っていません" in report.render()


def test_removing_dry_run_alone_is_not_enough() -> None:
    """13.6: **二重に明示的な操作を要する。**

    dry_run を外しただけで送れるなら、設定の取り違え1つで送信が始まる。
    """
    report = _call(dry_run=False, acknowledged=False)
    assert report.reached() is FirstSendStage.NOT_ACKNOWLEDGED
    assert "取り消せないことの確認がありません" in report.render()


def test_both_gates_together_are_required() -> None:
    """片方ずつでは通らないことを、両方の向きで固定する。"""
    assert _call(dry_run=True, acknowledged=False).reached() is FirstSendStage.DRY_RUN_ON
    assert _call(dry_run=True, acknowledged=True).reached() is FirstSendStage.DRY_RUN_ON
    assert _call(dry_run=False, acknowledged=False).reached() is FirstSendStage.NOT_ACKNOWLEDGED


def test_the_cap_is_one_and_is_not_configurable() -> None:
    """**1通目は1通である。** 設定から動かせる形にしない。"""
    assert FIRST_SEND_CAP == 1


# ---------------------------------------------------------------------------
# 報告が嘘をつかないこと
# ---------------------------------------------------------------------------


def test_a_report_that_contradicts_the_timeline_raises() -> None:
    """**進んだ証拠の連鎖が破れたら例外にする。**

    候補者は0件なのに文面ができている、という報告は嘘なので出さない。
    (門の2つは連鎖に含めない -- あれは入力であって進んだ証拠ではない。)
    """
    from jobmedley_scout.generation.scout_message import GeneratedMessage, GenerationOutcome

    report = FirstSendReport(
        dry_run=False,
        acknowledged=True,
        rows_seen=0,
        message=GeneratedMessage(
            candidate_id="1", outcome=GenerationOutcome.GENERATED, body="本文" * 300
        ),
    )
    with pytest.raises(ValueError, match="時系列と矛盾"):
        report.reached()


def test_no_candidate_is_named_rather_than_reported_as_sent() -> None:
    report = FirstSendReport(dry_run=False, acknowledged=True, rows_seen=0)
    assert report.reached() is FirstSendStage.NO_CANDIDATE
    assert "送る相手が取れませんでした" in report.render()


def test_a_missing_search_uuid_stops_the_send() -> None:
    """**記法が残ったまま送らない。** 送れば媒体へそのまま渡る (13.6)。

    ``assert_fully_filled`` も止めるが、そこまで行く前に理由を名前で報告する。
    """
    from jobmedley_scout.generation.scout_message import GeneratedMessage, GenerationOutcome

    message = GeneratedMessage(
        candidate_id="1", outcome=GenerationOutcome.GENERATED, body="本文" * 300
    )
    report = FirstSendReport(
        dry_run=False, acknowledged=True, rows_seen=1, message=message, search_uuid=None
    )
    assert report.reached() is FirstSendStage.NO_SEARCH_UUID
    assert "検索識別子" in report.render()


def test_the_provisional_success_set_is_2xx_and_says_so() -> None:
    """``success_statuses`` は1通送らないと分からない座標である (ラダー 4-3)。

    送る前に要求すると梯子が閉じるので暫定を使うが、**使ったことを報告に出す。**
    """
    assert PROVISIONAL_SUCCESS == frozenset(range(200, 300))


def test_acknowledging_while_dry_run_is_on_is_a_normal_state() -> None:
    """**門は順番ではなく独立である。**

    最初の実装はこの2つを単調性の連鎖に入れており、「dry_run は有効だが承知の印は
    ある」という正常な状態を矛盾として例外にしていた。運用者がチェックを入れたまま
    dry_run で試す、という一番ありふれた使い方が落ちる。検査が捕まえた。
    """
    report = FirstSendReport(dry_run=True, acknowledged=True)
    assert report.reached() is FirstSendStage.DRY_RUN_ON
