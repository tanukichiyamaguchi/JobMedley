"""8.1: 宛名・注記・定型文・署名・フッターと **書式** はコードが決める。"""

from __future__ import annotations

import pytest

from jobmedley_scout.errors import GenerationError
from jobmedley_scout.generation.assemble import (
    INTRO_BULLET,
    SECTION_RULE_CHAR,
    SYSTEM_GLYPHS,
    URL_ARROW,
    assemble,
)
from jobmedley_scout.generation.validators import _is_symbol
from jobmedley_scout.models.message import Introduction
from tests.generation.helpers import make_context, make_core


def test_code_adds_the_salutation_notes_signature_and_footer() -> None:
    message = assemble(make_core(), make_context())

    lines = message.body.splitlines()
    assert lines[0] == "田中太郎様"
    assert "本メールはご返信いただければ担当者に届きます。" in message.body
    assert "株式会社サンプル 採用支援チーム" in message.body
    assert "〒100-0001 東京都千代田区1-1-1" in message.body
    assert message.body.rstrip().endswith("https://job-medley.com/scout/")


def test_each_introduced_person_gets_its_own_block() -> None:
    core = make_core(
        introductions=(
            Introduction(person_id="p-001", blurb="鈴木は施設の立ち上げを担当しています。"),
            Introduction(person_id="p-002", blurb="高橋は採用面談を担当しています。"),
        )
    )

    message = assemble(core, make_context())

    blocks = [block for block in message.body.split("\n\n") if block.startswith(INTRO_BULLET)]
    assert len(blocks) == 2
    assert blocks[0] == f"{INTRO_BULLET} 鈴木は施設の立ち上げを担当しています。"


def test_internal_person_ids_never_reach_the_body() -> None:
    """person_id は突合用の識別子であって、受信者に見せるものではない。"""
    message = assemble(make_core(), make_context())

    assert "p-001" not in message.body


def test_url_placement_is_decided_by_code() -> None:
    message = assemble(make_core(), make_context())

    assert f"{URL_ARROW} 詳細はこちら: https://job-medley.com/scout/" in message.body
    assert message.body.count("https://job-medley.com/scout/") == 1


def test_no_introduction_heading_when_there_is_nobody_to_introduce() -> None:
    message = assemble(make_core(introductions=()), make_context())

    assert "ご紹介したいメンバー" not in message.body
    assert INTRO_BULLET not in message.body


def test_blank_line_runs_from_the_model_are_flattened() -> None:
    """空行の数まで LLM に委ねない。"""
    core = make_core(opening="はじめまして。\n\n\n\n担当の佐藤と申します。")

    message = assemble(core, make_context())

    assert "\n\n\n" not in message.body


def test_empty_generated_element_is_refused() -> None:
    with pytest.raises(GenerationError, match="closing"):
        assemble(make_core(closing="   "), make_context())


def test_subject_keys_are_carried_through_unchanged() -> None:
    context = make_context()

    message = assemble(make_core(), context)

    assert message.subject == context.subject.subject
    assert message.subject_norm == context.subject.subject_norm
    assert message.subject_prefix35 == context.subject.subject_prefix35


def test_every_decoration_glyph_is_declared_in_system_glyphs() -> None:
    """8.5: 装飾記号を足したのに SYSTEM_GLYPHS に足し忘れると、その記号で
    全メッセージが絵文字違反になり、毎回修正リトライが走る。"""
    message = assemble(make_core(), make_context())

    flagged = {char for char in f"{message.subject}\n{message.body}" if _is_symbol(char)}

    assert flagged <= SYSTEM_GLYPHS, f"未申告の装飾記号: {sorted(flagged - SYSTEM_GLYPHS)}"
    # 実際に装飾を使っていること (使っていなければこの検査は無意味になる)。
    assert {INTRO_BULLET, SECTION_RULE_CHAR, URL_ARROW} <= flagged
