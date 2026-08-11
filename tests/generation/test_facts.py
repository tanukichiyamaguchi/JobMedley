"""8.3 対策1: 事実が渡っていること、そして「無い」ことが明示されていること。"""

from __future__ import annotations

from jobmedley_scout.generation.facts import (
    FACT_LABELS,
    UNDISCLOSED,
    PromptFacts,
    build_facts,
)
from tests.generation.helpers import make_candidate, make_resume


def test_every_field_has_a_label_and_none_is_dropped() -> None:
    """項目を足してラベルを足し忘れる (= プロンプトに出ない) を防ぐ。"""
    assert set(FACT_LABELS) == set(PromptFacts.model_fields)


def test_absent_facts_are_rendered_as_undisclosed() -> None:
    """値が無い項目も「非公開」として必ず渡す。項目ごと落とすと、モデルは
    「無い」のか「渡し忘れ」なのかを区別できず補完 (=創作) を始める。"""
    facts = build_facts(make_candidate(display_name="田中太郎"))
    lines = facts.render_for_prompt().splitlines()

    # 行数 = 項目数。1つでも落ちていればここで落ちる。
    assert len(lines) == len(PromptFacts.model_fields)

    # 氏名以外はすべて未取得なので「非公開」になる。
    undisclosed_lines = [line for line in lines if line.endswith(f": {UNDISCLOSED}")]
    assert len(undisclosed_lines) == len(PromptFacts.model_fields) - 1
    assert "氏名: 田中太郎" in lines


def test_every_label_appears_exactly_once_in_the_prompt_block() -> None:
    facts = build_facts(make_candidate())
    rendered = facts.render_for_prompt()
    for field, label in FACT_LABELS.items():
        assert f"{label}: " in rendered, f"{field} がプロンプトに出ていません"


def test_disclosed_facts_are_rendered_with_their_values() -> None:
    resume = make_resume(
        current_company="株式会社ケアネット",
        current_occupation="介護福祉士",
        previous_company="医療法人サンプル会",
        previous_occupation="ヘルパー",
        experienced_industries=("介護", "医療"),
        school="サンプル大学",
        desired_locations=("東京都", "神奈川県"),
    )
    facts = build_facts(make_candidate(resume=resume))
    rendered = facts.render_for_prompt()

    assert "現在の勤務先: 株式会社ケアネット" in rendered
    assert "現在の職種: 介護福祉士" in rendered
    assert "現在の勤続年数: 3年" in rendered
    assert "過去の勤務先: 医療法人サンプル会" in rendered
    assert "経験業界: 介護、医療" in rendered
    assert "学校名: サンプル大学" in rendered
    assert "希望勤務地: 東京都、神奈川県" in rendered

    assert "current_company" in facts.disclosed_field_names()
    assert "specialty" in facts.undisclosed_field_names()


def test_current_and_previous_employments_are_kept_apart() -> None:
    """現職の推測をしない。is_current が立っていなければ現職は非公開のまま。"""
    resume = make_resume(previous_company="株式会社むかし", previous_occupation="営業")
    facts = build_facts(make_candidate(resume=resume))

    assert facts.current_company.rendered == UNDISCLOSED
    assert facts.previous_employers.rendered == "株式会社むかし"


def test_language_field_is_deliberately_not_in_the_prompt() -> None:
    """7.2: 語学欄は外国語判定にのみ使う欄。文面生成に渡すと国籍・母語への
    言及を誘発するため、意図的に渡していない。"""
    resume = make_resume(language_text="英語ネイティブ / 日本語ビジネスレベル")
    facts = build_facts(make_candidate(resume=resume))

    assert "ネイティブ" not in facts.render_for_prompt()


def test_duplicate_values_are_collapsed_and_order_is_kept() -> None:
    resume = make_resume(experienced_industries=("介護", "介護", "医療"))
    facts = build_facts(make_candidate(resume=resume))

    assert facts.experienced_industries.values == ("介護", "医療")


def test_blank_strings_count_as_absent() -> None:
    resume = make_resume(current_company="   ", current_occupation="介護福祉士")
    facts = build_facts(make_candidate(resume=resume))

    assert facts.current_company.disclosed is False
    assert facts.current_company.rendered == UNDISCLOSED
