"""運用者のプロンプトで1通書かせ、**検査に通るまで直させる** 層。

モデルの自己チェックは信用しない (8.5)。プロンプトに「違反があれば書き直す」と
書いてあっても、書いてあることは守られることではない。守らせるのは検査である。
"""

from __future__ import annotations

from typing import Any

from jobmedley_scout.config.schema import LlmConfig
from jobmedley_scout.generation.clinic import load_clinic_facts
from jobmedley_scout.generation.facts import UNDISCLOSED
from jobmedley_scout.generation.scout_body import APPLY_BUTTON, HEADLINE, BodyViolationKind
from jobmedley_scout.generation.scout_message import (
    BODY_SCHEMA,
    OUTPUT_ENVELOPE,
    GeneratedMessage,
    GenerationOutcome,
    build_prompt,
    candidate_slots,
    generate_scout_body,
)
from jobmedley_scout.models.candidate import Candidate, ResumeFacts
from tests.generation.test_clinic_facts import CLINIC_PATH, PROMPT_PATH

CODE = "01613058"
ADDRESS = "〒214-0001 神奈川県川崎市多摩区菅４丁目３−３２ ２階"

CONFIG = LlmConfig(
    model="claude-sonnet-5", max_tokens=16000, thinking_enabled=True, effort="medium", max_retries=3
)


def _candidate(**overrides: Any) -> Candidate:
    base: dict[str, Any] = {
        "candidate_id": "3323741",
        "raw_id_observed": "3323741",
        "member_code": CODE,
        "residence": "神奈川県川崎市宮前区",
        "resume": ResumeFacts(
            qualifications=("歯科衛生士", "自動車運転免許"),
            experienced_occupations=("歯科衛生士(3年)",),
            desired_occupations=("歯科衛生士",),
            desired_features=("社会保険完備",),
        ),
    }
    base.update(overrides)
    return Candidate(**base)


def _good_body() -> str:
    filler = "当院は患者担当制で、一人の患者様に向き合えます。" * 25
    return (
        f"{HEADLINE}\n{CODE}様（システム上お名前が表示されず、会員番号でのご挨拶と"
        f"なる失礼をお許しください）\n{filler}\n"
        f"ご興味をお持ちいただけましたら、{APPLY_BUTTON}ボタンを押してください。\n"
        f"ヤガサキ歯科医院 院長 矢ケ崎 隆信"
    )


class _Fake:
    """A stand-in for the Anthropic client. **実APIは呼ばない** (13.4)。"""

    def __init__(self, bodies: list[str]) -> None:
        self._bodies = bodies
        self.systems: list[str] = []
        self.turns: list[int] = []

    @property
    def messages(self) -> Any:
        return self

    def create(self, **kwargs: Any) -> Any:
        self.systems.append(kwargs.get("system", ""))
        self.turns.append(len(kwargs.get("messages", ())))
        body = self._bodies.pop(0)
        import json

        class _Block:
            type = "text"
            text = json.dumps({"body": body}, ensure_ascii=False)

        class _Usage:
            input_tokens = 100
            output_tokens = 50
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        class _Response:
            content = (_Block(),)
            usage = _Usage()
            stop_reason = "end_turn"
            model = "claude-sonnet-5"

        return _Response()


# ---------------------------------------------------------------------------
# 差し込み
# ---------------------------------------------------------------------------


def test_every_slot_is_passed_even_when_the_value_is_missing() -> None:
    """**項目ごと落とさない** (8.3 対策1)。

    落とすと、モデルからは「その事実が無い」のか「渡し忘れた」のかが区別できず、
    後者だと解釈した瞬間に補完 (=創作) が始まる。
    """
    slots = candidate_slots(Candidate(candidate_id="1", raw_id_observed="1"))
    assert set(slots) == {
        "MEMBER_CODE",
        "RESIDENCE",
        "NEAREST_STATION",
        "QUALIFICATIONS",
        "CAREER_YEARS",
        "CAREER_SUMMARY",
        "DESIRED_CONDITIONS",
        "SELF_PR",
        "SCOUT_HISTORY",
        "LAST_SENT_AT",
        "LAST_RESPONSE",
    }
    assert all(value == UNDISCLOSED for value in slots.values())


def test_the_nearest_station_is_never_filled_in() -> None:
    """**駅名は取れない。** 空で渡すことが、そのまま推測の禁止を効かせる。

    プロンプトは「路線名・駅名は明記されている場合を除いて書かない」と定めて
    いるので、ここを住所から埋めるとその禁止が無効になる。
    """
    assert candidate_slots(_candidate())["NEAREST_STATION"] == UNDISCLOSED


def test_career_years_passes_the_raw_field_rather_than_recomputing() -> None:
    """**年数を数え直さない。**

    「歯科衛生士(3年)」から年数だけ抜いて足し合わせると、重複や併行の扱いを
    こちらが決めることになり、その解釈が事実として文面に出る (6.4)。
    """
    assert candidate_slots(_candidate())["CAREER_YEARS"] == "歯科衛生士(3年)"


def test_the_real_prompt_fills_with_the_real_clinic_and_a_candidate() -> None:
    """実物どうしで通ること。**{{...}} が1つも残らない。**"""
    prompt = build_prompt(
        PROMPT_PATH.read_text(encoding="utf-8"), load_clinic_facts(CLINIC_PATH), _candidate()
    )
    assert "{{" not in prompt
    assert "ヤガサキ歯科医院" in prompt
    assert "神奈川県川崎市宮前区" in prompt


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def test_a_candidate_without_a_member_code_is_never_written_for() -> None:
    """**書かせる前に止める。**

    このプロンプトは会員番号で呼びかけると決めている (STEP3 (2))。番号が無いまま
    書かせると、モデルは宛名を空欄にするか番号を創作する。どちらも候補者へ届く。
    """
    client = _Fake([])
    result = generate_scout_body(
        client, config=CONFIG, prompt="p", candidate=_candidate(member_code=None)
    )
    assert result.outcome is GenerationOutcome.NO_MEMBER_CODE
    assert not result.sendable
    assert result.attempts == 0, "LLM を1回も呼んでいないこと"


def test_a_clean_body_comes_back_on_the_first_attempt() -> None:
    client = _Fake([_good_body()])
    result = generate_scout_body(
        client, config=CONFIG, prompt="p", candidate=_candidate(), clinic_address=ADDRESS
    )
    assert result.outcome is GenerationOutcome.GENERATED
    assert result.sendable
    assert result.attempts == 1
    assert result.usage.output_tokens == 50


def test_a_violating_body_is_sent_back_for_a_rewrite() -> None:
    """**検査が守らせる。** プロンプトに書いてあることは保証ではない (8.5)。"""
    bad = _good_body().replace(HEADLINE, "こんにちは")
    client = _Fake([bad, _good_body()])
    result = generate_scout_body(
        client, config=CONFIG, prompt="p", candidate=_candidate(), clinic_address=ADDRESS
    )
    assert result.outcome is GenerationOutcome.GENERATED
    assert result.attempts == 2
    # 2回目は違反を伝える往復が積まれている。
    assert client.turns[1] > client.turns[0]
    # トークンは足し合わされる (13.1 のコスト観測)。
    assert result.usage.output_tokens == 100


def test_a_body_that_never_becomes_valid_is_not_sendable() -> None:
    """**直らなければ送らない。** 違反を持ったまま返る (8.2: 例外にしない)。"""
    bad = _good_body().replace(HEADLINE, "こんにちは")
    client = _Fake([bad, bad, bad])
    result = generate_scout_body(
        client,
        config=CONFIG,
        prompt="p",
        candidate=_candidate(),
        clinic_address=ADDRESS,
        max_attempts=3,
    )
    assert result.outcome is GenerationOutcome.STILL_INVALID
    assert not result.sendable
    assert result.attempts == 3
    assert BodyViolationKind.MISSING_HEADLINE in [v.kind for v in result.violations]


def test_the_address_check_is_wired_into_generation() -> None:
    """渡した住所が本文に出たら、それも書き直しの対象になること。"""
    leaky = _good_body() + "\n住所は菅4丁目3-32です。"
    client = _Fake([leaky, leaky])
    result = generate_scout_body(
        client,
        config=CONFIG,
        prompt="p",
        candidate=_candidate(),
        clinic_address=ADDRESS,
        max_attempts=2,
    )
    assert result.outcome is GenerationOutcome.STILL_INVALID
    assert BodyViolationKind.STREET_ADDRESS_LEAKED in [v.kind for v in result.violations]


def test_an_llm_failure_is_counted_rather_than_raised() -> None:
    """8.2: 「生成失敗はログではなく **集計値として出力**」。"""

    class _Broken:
        @property
        def messages(self) -> Any:
            return self

        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("400 invalid thinking parameter")

    result = generate_scout_body(_Broken(), config=CONFIG, prompt="p", candidate=_candidate())
    assert result.outcome is GenerationOutcome.LLM_FAILED
    # **元の型まで残す。** 型名だけだと、8.2 の事故 (API仕様変更で全件400) が
    # 「GenerationError が N件」としか見えず、仕様変更なのかこちらの不備なのかが
    # 報告から決まらない。
    assert result.failure == "GenerationError(RuntimeError)"
    assert not result.sendable


def test_the_rewrite_note_never_quotes_the_body_back() -> None:
    """会話に本文を積むと入力が回ごとに膨らみ、13.1 のコスト上限が意味を失う。"""
    bad = _good_body().replace(HEADLINE, "こんにちは")
    client = _Fake([bad, bad])
    generate_scout_body(client, config=CONFIG, prompt="p", candidate=_candidate(), max_attempts=2)
    assert client.turns[1] == 3, "往復は user/assistant/user の3つで足りる"


def test_the_output_envelope_is_appended_without_touching_the_operators_prompt() -> None:
    """**運用者のプロンプトは1文字も変えない。** 足すのは返し方だけである。"""
    client = _Fake([_good_body()])
    generate_scout_body(client, config=CONFIG, prompt="運用者の指示", candidate=_candidate())
    assert client.systems[0].startswith("運用者の指示")
    assert client.systems[0].endswith(OUTPUT_ENVELOPE)
    # プロンプトの原文にこの文言は入っていない。
    assert OUTPUT_ENVELOPE not in PROMPT_PATH.read_text(encoding="utf-8")


def test_the_schema_has_exactly_one_field() -> None:
    """**欄を増やすとモデルは埋めようとして本文から材料を移す。**"""
    assert list(BODY_SCHEMA["properties"]) == ["body"]
    assert BODY_SCHEMA["additionalProperties"] is False


def test_a_fresh_result_is_not_sendable_by_default() -> None:
    assert not GeneratedMessage(candidate_id="1", outcome=GenerationOutcome.LLM_FAILED).sendable
