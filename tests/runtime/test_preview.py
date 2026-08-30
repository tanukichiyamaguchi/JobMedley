"""1通目の下見。**本文も会員番号も、ログには1文字も出さない** (13.2)。

段階4-3 は「1通だけ、文面を確認してから」である。確認できなければ進めないが、
本文には確認に要るものが最初から入っている -- 会員番号での宛名と居住地の
市区町村。だから場所を分ける: ログには数と種別、本文は成果物へ。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobmedley_scout.config.schema import LlmConfig
from jobmedley_scout.generation.scout_body import APPLY_BUTTON, HEADLINE
from jobmedley_scout.generation.scout_message import GeneratedMessage, GenerationOutcome
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.runtime.commands.ingest import IngestReport
from jobmedley_scout.runtime.commands.preview import (
    PreviewReport,
    PreviewStage,
    _reason_for,
    _write,
)

CODE = "01613058"
CITY = "神奈川県川崎市宮前区"
CONFIG = LlmConfig(
    model="claude-sonnet-5", max_tokens=16000, thinking_enabled=True, effort="medium", max_retries=3
)


def _body() -> str:
    filler = "当院は患者担当制で、一人の患者様に向き合えます。" * 25
    return (
        f"{HEADLINE}\n{CODE}様（システム上お名前が表示されず、会員番号でのご挨拶と"
        f"なる失礼をお許しください）\n{CITY}からですと、お車でおよそ30分前後かと思います。\n"
        f"{filler}\nご興味をお持ちいただけましたら、{APPLY_BUTTON}ボタンを押してください。\n"
        f"ヤガサキ歯科医院 院長 矢ケ崎 隆信"
    )


def _ready(tmp_path: Path) -> PreviewReport:
    message = GeneratedMessage(
        candidate_id="3323741",
        outcome=GenerationOutcome.GENERATED,
        body=_body(),
        attempts=1,
        requests=1,
    )
    return PreviewReport(
        ingest=IngestReport(),
        rows=1,
        usable=True,
        message=message,
        written_to=tmp_path / "scout-preview.md",
    )


def test_the_log_never_carries_the_body_or_the_member_code(tmp_path: Path) -> None:
    """**ここが下見の安全性の全部である。**

    本文には会員番号での宛名と居住地が入っている。ログへ出せば、それは
    13.2 が禁じているものが Actions に残るということである。
    """
    said = _ready(tmp_path).render()
    assert CODE not in said, "会員番号がログに出ている"
    assert CITY not in said, "居住地がログに出ている"
    assert HEADLINE not in said, "本文がログに出ている"
    assert "当院は患者担当制" not in said
    # かわりに、数と種別は出る。
    assert "書き直し: 1 回" in said
    assert "本文の長さ:" in said
    assert "scout-preview.md" in said


def test_the_report_says_out_loud_that_nothing_was_sent(tmp_path: Path) -> None:
    assert "送信を1件も行っていません" in _ready(tmp_path).render()


def test_the_artifact_carries_what_the_operator_needs_to_judge(tmp_path: Path) -> None:
    """**成果物には出す。** 運用者は本文を読まなければ判断できない。"""
    destination = tmp_path / "preview" / "scout-preview.md"
    candidate = Candidate(
        candidate_id="3323741", raw_id_observed="3323741", member_code=CODE, residence=CITY
    )
    written = _write(
        destination,
        GeneratedMessage(
            candidate_id="3323741", outcome=GenerationOutcome.GENERATED, body=_body(), attempts=1
        ),
        candidate,
    )
    assert written == destination
    text = destination.read_text(encoding="utf-8")
    assert HEADLINE in text
    assert CODE in text
    assert "まだ送っていません" in text


def test_a_write_that_fails_is_not_reported_as_success(tmp_path: Path) -> None:
    """**書けなかったことを黙らない。**

    黙ると、報告は「できました」と言うのに落とすものが無い、という食い違いに
    なる (原則2)。
    """
    blocked = tmp_path / "afile"
    blocked.write_text("x", encoding="utf-8")
    assert (
        _write(
            blocked / "nested" / "out.md",
            GeneratedMessage(
                candidate_id="1", outcome=GenerationOutcome.GENERATED, body="x", attempts=1
            ),
            Candidate(candidate_id="1", raw_id_observed="1", member_code=CODE, residence=CITY),
        )
        is None
    )


def test_a_candidate_missing_either_required_field_is_skipped() -> None:
    """**どちらが欠けても書かせない。** 理由は種別だけを返す (13.2)。"""
    assert (
        _reason_for(Candidate(candidate_id="1", raw_id_observed="1", residence=CITY))
        == "会員番号が無い"
    )
    assert (
        _reason_for(Candidate(candidate_id="1", raw_id_observed="1", member_code=CODE))
        == "居住地が無い"
    )
    assert (
        _reason_for(
            Candidate(candidate_id="1", raw_id_observed="1", member_code=CODE, residence=CITY)
        )
        is None
    )


def test_the_skip_report_counts_people_without_naming_them() -> None:
    report = PreviewReport(ingest=IngestReport(), rows=3, skipped={"居住地が無い": 3})
    said = report.render()
    assert "居住地が無い: 3 名" in said
    assert "生成に回せる候補者が居ませんでした" in said


def test_the_stage_chain_reports_the_stage_actually_reached(tmp_path: Path) -> None:
    """**単調性。** 後の段の条件は、前の段の条件に含まれていなければならない。"""
    assert PreviewReport(ingest=IngestReport()).reached() is PreviewStage.NO_ROWS
    assert PreviewReport(ingest=IngestReport(), rows=1).reached() is (
        PreviewStage.NO_USABLE_CANDIDATE
    )
    assert PreviewReport(ingest=IngestReport(), rows=1, usable=True).reached() is (
        PreviewStage.NOT_GENERATED
    )
    assert _ready(tmp_path).reached() is PreviewStage.READY


def test_a_state_that_contradicts_the_timeline_raises_instead_of_lying() -> None:
    """**報告を嘘にしないために止める。** 行が無いのに文面ができることはない。"""
    impossible = PreviewReport(
        ingest=IngestReport(),
        rows=0,
        usable=True,
        message=GeneratedMessage(
            candidate_id="1", outcome=GenerationOutcome.GENERATED, body="x", attempts=1
        ),
    )
    with pytest.raises(ValueError, match="時系列と矛盾"):
        impossible.reached()


def test_a_fallback_that_fired_is_never_silent(tmp_path: Path) -> None:
    """常時発火しているなら API 側の仕様が変わった合図である (8.2)。"""
    report = _ready(tmp_path)
    assert report.message is not None
    report.message = GeneratedMessage(
        candidate_id="1",
        outcome=GenerationOutcome.GENERATED,
        body=_body(),
        attempts=1,
        used_fallback=True,
    )
    assert "フォールバックが発火" in report.render()


def test_violations_reach_the_log_as_kinds_not_as_text(tmp_path: Path) -> None:
    """直す手掛かりは要る。**本文は要らない。**"""
    from jobmedley_scout.generation.scout_body import BodyViolation, BodyViolationKind

    report = PreviewReport(
        ingest=IngestReport(),
        rows=1,
        usable=True,
        message=GeneratedMessage(
            candidate_id="1",
            outcome=GenerationOutcome.STILL_INVALID,
            body=_body(),
            attempts=3,
            violations=(
                BodyViolation(
                    kind=BodyViolationKind.MISSING_HEADLINE,
                    detail="1行目の表示がありません。",
                    evidence="(1行目が表示ではありません)",
                ),
            ),
        ),
    )
    said = report.render()
    assert "missing_headline" in said
    assert CODE not in said
    assert HEADLINE not in said
    assert "送れる文面はできていません" in said


def test_the_preview_never_imports_the_send_path() -> None:
    """**送信APIを呼びようがないこと。** 受け取ってもいない (13.6)。"""
    source = Path("src/jobmedley_scout/runtime/commands/preview.py").read_text(encoding="utf-8")
    assert "send_message" not in source
    assert "api.send" not in source
