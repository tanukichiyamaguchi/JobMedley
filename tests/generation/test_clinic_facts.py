"""医院情報の差し込み。**埋め忘れた欄が候補者へ届かないようにする。**

プロンプトの ``{{CLINIC_ADDRESS}}`` が埋まらないままモデルへ渡ると、モデルは
記法を読まない。医院名は実在するので **それらしい住所を書く**。原則3 が禁じて
いる推測が、こちらの取りこぼしから始まる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.generation.clinic import (
    NOT_REQUIRED_TEXT,
    NOT_REQUIRED_TOKEN,
    UNRESOLVED_TOKEN,
    fill,
    load_clinic_facts,
    not_required_slots,
    slots_in,
    unresolved_slots,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLINIC_PATH = REPO_ROOT / "config" / "clinic.yaml"
PROMPT_PATH = REPO_ROOT / "config" / "prompts" / "scout_dental_hygienist.md"

#: プロンプトの【3】医院情報が持つ欄。**運用者のプロンプトが決めている。**
CLINIC_SLOT_NAMES = frozenset(
    {
        "CLINIC_NAME",
        "DIRECTOR_NAME",
        "CLINIC_ADDRESS",
        "CLINIC_ACCESS",
        "CLINIC_PARKING",
        "CLINIC_TREATMENTS",
        "CLINIC_STAFF",
        "CLINIC_TRAINING",
        "CLINIC_WORKSTYLE",
        "CLINIC_NOTES",
    }
)


def test_the_config_covers_exactly_the_prompts_clinic_slots() -> None:
    """**過不足のどちらも失敗にする。**

    足りなければ差し込みが止まる。余っていれば、プロンプトから消えた欄に
    書いた事実が誰にも読まれないまま残り、次に読む人間を誤らせる。
    """
    facts = load_clinic_facts(CLINIC_PATH)
    in_prompt = set(slots_in(PROMPT_PATH.read_text(encoding="utf-8")))
    assert CLINIC_SLOT_NAMES <= in_prompt, "プロンプト側から医院情報の欄が消えている"
    assert set(facts) == CLINIC_SLOT_NAMES


def test_the_operators_declared_unnecessary_fields_are_recorded_as_such() -> None:
    """運用者は「住所・アクセスは必要ない」と明示した。**宿題ではない。**"""
    facts = load_clinic_facts(CLINIC_PATH)
    assert not_required_slots(facts) == ("CLINIC_ACCESS", "CLINIC_ADDRESS", "CLINIC_PARKING")
    # 未確認の欄は無い。**「聞き忘れ」と「要らないと言われた」を分けてある。**
    assert unresolved_slots(facts) == ()


def test_an_unresolved_field_stops_the_prompt_rather_than_guessing() -> None:
    """**止めるのが正しい。** 埋めた内容がそのまま候補者へ届く。"""
    with pytest.raises(ConfigError) as excinfo:
        fill("所在地：{{CLINIC_ADDRESS}}", {"CLINIC_ADDRESS": UNRESOLVED_TOKEN}, used_by="t")
    assert "CLINIC_ADDRESS" in str(excinfo.value)


def test_a_not_required_field_passes_but_says_it_is_absent() -> None:
    """**空文字では渡さない。**

    空文字は「その欄が無い」とも「渡し忘れた」とも読める。後者と解釈された
    瞬間にモデルの補完が始まる (generation.facts の UNDISCLOSED と同じ理由)。
    """
    out = fill("所在地：{{CLINIC_ADDRESS}}", {"CLINIC_ADDRESS": NOT_REQUIRED_TOKEN}, used_by="t")
    assert out == f"所在地：{NOT_REQUIRED_TEXT}"
    assert NOT_REQUIRED_TOKEN not in out


def test_a_slot_with_no_value_is_caught_even_though_no_field_is_unresolved() -> None:
    """**未確定の検査だけでは捕まらない穴。**

    プロンプト側に欄が増えたとき、こちらの値には現れないので UNRESOLVED では
    引っかからない。残った ``{{...}}`` を別に見る。
    """
    with pytest.raises(ConfigError) as excinfo:
        fill(
            "{{CLINIC_NAME}} / {{BRAND_NEW_FIELD}}",
            {"CLINIC_NAME": "ヤガサキ歯科医院"},
            used_by="t",
        )
    assert "BRAND_NEW_FIELD" in str(excinfo.value)


def test_the_real_config_fills_the_real_prompts_clinic_section() -> None:
    """実物どうしで通ること。**候補者側の欄はまだ残ってよい** (生成時に埋まる)。"""
    facts = load_clinic_facts(CLINIC_PATH)
    template = PROMPT_PATH.read_text(encoding="utf-8")
    partly = fill(
        template + "{{CLINIC_NAME}}", {**facts, **_candidate_stubs(template)}, used_by="t"
    )
    assert "{{" not in partly
    assert "ヤガサキ歯科医院" in partly
    assert "矢ケ崎 隆信" in partly
    # 給与は運用者が「必要ない」と言った項目。**渡さないので書かれようがない。**
    assert "月給" not in "".join(facts.values())


def test_no_clinic_fact_leaks_a_forbidden_employment_term() -> None:
    """正社員のみの募集。**パート/アルバイト/時給 は渡す材料にも入れない。**

    出力側の検査 (generation.scout_body) だけに頼ると、材料に入っている限り
    モデルは書こうとし、修正リトライを無駄に焼く。入口で断つほうが安い。
    """
    joined = "".join(load_clinic_facts(CLINIC_PATH).values())
    for term in ("パート", "アルバイト", "時給"):
        assert term not in joined


def _candidate_stubs(template: str) -> dict[str, str]:
    """候補者側の欄を仮で埋める。**この関数は試験用であり、本番経路には無い。**"""
    return {name: "（試験用）" for name in slots_in(template) if name not in CLINIC_SLOT_NAMES}
