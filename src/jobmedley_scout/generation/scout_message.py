"""運用者のプロンプトで1通のスカウト文を書かせ、**検査に通るまで直させる**。

ここが :mod:`generation.clinic` (医院情報の差し込み) と
:mod:`generation.scout_body` (出力の検査) を繋ぐ層である。

**モデルの自己チェックは信用しない。** プロンプトの STEP4 に「確認し、違反が
あれば書き直してから出力する」と書いてあっても、書いてあることは守られること
ではない (8.5)。守らせるのは検査であり、違反したら **違反の一覧を渡して
書き直させる**。それでも直らなければ **送らない**。

設計上の要点が4つある。

1. **本文を構造化出力で受け取る。** プロンプトは「本文のみを出力する。前置き、
   解説、補足説明は一切付けない」と命じているが、これも指示であって保証では
   ない。``{"body": "..."}`` の形で受け取れば、「以下がスカウトメールです」の
   ような前置きが本文に混ざる経路が構文的に消える。
2. **会員番号が無ければ生成しない。** このプロンプトは会員番号で呼びかけると
   決めている (STEP3 (2))。番号が無いまま書かせると、モデルは宛名を埋める
   方法を自分で探す -- 「〇〇様」と空欄を作るか、番号らしきものを創作する。
   どちらも候補者へ届く。**書かせる前に止める。**
3. **リトライの回数は設定が持つ** (``safety.max_llm_requests_per_message``)。
   13.1: リトライ×フォールバック×修正リトライで膨らむコストを構造で止める。
4. **失敗は件数で返す。** 8.2: 「生成失敗はログではなく集計値として出力」。
   例外を投げずに :class:`GeneratedMessage` が理由を持って返る。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from jobmedley_scout.config.schema import LlmConfig
from jobmedley_scout.generation.clinic import fill
from jobmedley_scout.generation.facts import UNDISCLOSED
from jobmedley_scout.generation.llm_client import AnthropicLike, TokenUsage, call_structured
from jobmedley_scout.generation.scout_body import BodyViolation, validate_body
from jobmedley_scout.models.candidate import Candidate

#: 本文を受け取る構造化出力の形。**1つの欄しか無い。**
#:
#: 欄を増やすと、モデルは埋めようとして本文から材料を移す。プロンプトが求めて
#: いるのは本文だけである。
BODY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"body": {"type": "string", "description": "スカウトメールの本文だけ"}},
    "required": ["body"],
    "additionalProperties": False,
}

#: プロンプトの末尾に足す **出力の封筒だけ** の指示。
#:
#: **運用者のプロンプト本文は1文字も変えていない** (config/prompts/*.md)。
#: ここで足すのは「どう返すか」であって「何を書くか」ではない。内容の決まりは
#: すべて運用者のプロンプトが持っている。
OUTPUT_ENVELOPE: Final[str] = (
    "\n\n━━━━━━━━━━━━━━━━━━\n"
    "■ 返し方\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "本文を JSON の body という欄に入れて返すこと。"
    "body 以外の欄は作らない。前置きや解説を body の中に入れない。\n"
)


class GenerationOutcome(StrEnum):
    """1通の生成が辿り着いた先。**時系列の順に並んでいる。**"""

    NO_MEMBER_CODE = "no_member_code"
    LLM_FAILED = "llm_failed"
    STILL_INVALID = "still_invalid"
    GENERATED = "generated"


@dataclass(frozen=True)
class GeneratedMessage:
    """One generation attempt. **例外ではなく、理由を持って返る** (8.2)。"""

    candidate_id: str
    outcome: GenerationOutcome
    body: str = ""
    #: 最後まで残った違反。``GENERATED`` なら空である。
    violations: tuple[BodyViolation, ...] = ()
    #: LLM を呼んだ回数 (修正リトライを含む)。13.1 のコスト観測用。
    attempts: int = 0
    usage: TokenUsage = TokenUsage(0, 0)
    #: LLM が例外で落ちたときの型名。**握りつぶさず理由を残す。**
    failure: str = ""

    @property
    def sendable(self) -> bool:
        return self.outcome is GenerationOutcome.GENERATED and bool(self.body)


def candidate_slots(
    candidate: Candidate,
    *,
    scout_history: str = "",
    last_sent_at: str = "",
    last_response: str = "",
) -> dict[str, str]:
    """The prompt's candidate slots. **値が無い欄も必ず渡す** (8.3 対策1)。

    項目ごと落とすと、モデルからは「その事実が無い」のか「渡し忘れた」のかが
    区別できず、後者だと解釈した瞬間に補完 (=創作) が始まる。無い項目は
    :data:`generation.facts.UNDISCLOSED` と明示する。

    **氏名の欄は無い。** この媒体に氏名は無く、宛名は会員番号で書くと運用者が
    決めている (プロンプト STEP3 (2))。
    """
    resume = candidate.resume
    years = _career_years(resume.experienced_occupations)
    return {
        "MEMBER_CODE": _or_undisclosed(candidate.member_code),
        "RESIDENCE": _or_undisclosed(candidate.residence),
        # **最寄駅は取れない。** 一覧にもレジュメにも欄が無い。空で渡すことが、
        # そのままプロンプトの「推測で路線を書かない」を効かせる。
        "NEAREST_STATION": UNDISCLOSED,
        "QUALIFICATIONS": _or_undisclosed(_join(resume.qualifications)),
        "CAREER_YEARS": _or_undisclosed(years),
        "CAREER_SUMMARY": _or_undisclosed(_join(resume.experienced_occupations)),
        "DESIRED_CONDITIONS": _or_undisclosed(
            _join(
                (*resume.desired_occupations, *resume.desired_locations, *resume.desired_features)
            )
        ),
        "SELF_PR": _or_undisclosed(resume.self_pr),
        "SCOUT_HISTORY": scout_history or UNDISCLOSED,
        "LAST_SENT_AT": last_sent_at or UNDISCLOSED,
        "LAST_RESPONSE": last_response or UNDISCLOSED,
    }


def _or_undisclosed(value: str | None) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else UNDISCLOSED


def _join(values: Sequence[str]) -> str | None:
    kept = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    return "、".join(dict.fromkeys(kept)) or None


def _career_years(occupations: Sequence[str]) -> str | None:
    """経験職種の欄はそのまま渡す。**年数だけを抜き出して数え直さない。**

    媒体は「歯科衛生士(3年)」のように職種と年数を1つの文字列で返す。ここで
    年数だけを取り出して足し合わせると、重複や併行の扱いを **こちらが決める**
    ことになり、その解釈が事実として文面に出る (6.4 と同じ事故の形)。
    """
    return _join(occupations)


def build_prompt(
    template: str,
    clinic: Mapping[str, str],
    candidate: Candidate,
    *,
    scout_history: str = "",
    last_sent_at: str = "",
    last_response: str = "",
) -> str:
    """Fill the operator's prompt. Raises rather than emitting a half-filled one.

    差し込み漏れは :func:`generation.clinic.fill` が止める -- 残った ``{{...}}``
    をモデルは記法として読まず、**知っている風に書く**。
    """
    slots = {
        **dict(clinic),
        **candidate_slots(
            candidate,
            scout_history=scout_history,
            last_sent_at=last_sent_at,
            last_response=last_response,
        ),
    }
    return fill(template, slots, used_by="generation.scout_message.build_prompt")


def _violation_note(violations: Sequence[BodyViolation]) -> str:
    """The rewrite instruction. **本文は引用しない** -- 違反の説明だけを渡す。"""
    lines = ["いま書いた本文には、次の決まり違反があります。直して書き直してください。", ""]
    for violation in violations:
        lines.append(f"・{violation.detail}（該当: {violation.evidence}）")
    lines.append("")
    lines.append("直した本文だけを、同じ形 (body の欄) で返してください。")
    return "\n".join(lines)


def generate_scout_body(
    client: AnthropicLike,
    *,
    config: LlmConfig,
    prompt: str,
    candidate: Candidate,
    clinic_address: str = "",
    max_attempts: int = 3,
) -> GeneratedMessage:
    """Write one scout body, retrying while it breaks the operator's rules.

    **会員番号が無ければ書かせない。** 宛名の手掛かりが無いままモデルに任せると、
    空欄の宛名か、創作された番号のどちらかが候補者へ届く。
    """
    member_code = (candidate.member_code or "").strip()
    if not member_code:
        return GeneratedMessage(
            candidate_id=candidate.candidate_id,
            outcome=GenerationOutcome.NO_MEMBER_CODE,
        )

    system = prompt + OUTPUT_ENVELOPE
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": "上の指示どおり、この求職者へのスカウト文を書いてください。"}
    ]
    total = TokenUsage(0, 0)
    attempts = 0
    violations: tuple[BodyViolation, ...] = ()
    body = ""

    for _ in range(max(1, max_attempts)):
        attempts += 1
        try:
            result = call_structured(
                client,
                config=config,
                system=system,
                messages=messages,
                schema=BODY_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001 -- 8.2: 落とさず、件数として返す
            return GeneratedMessage(
                candidate_id=candidate.candidate_id,
                outcome=GenerationOutcome.LLM_FAILED,
                attempts=attempts,
                usage=total,
                failure=_failure_name(exc),
            )
        total = _add(total, result.usage)
        raw = result.data.get("body")
        body = raw.strip() if isinstance(raw, str) else ""
        violations = validate_body(body, member_code=member_code, clinic_address=clinic_address)
        if not violations:
            return GeneratedMessage(
                candidate_id=candidate.candidate_id,
                outcome=GenerationOutcome.GENERATED,
                body=body,
                attempts=attempts,
                usage=total,
            )
        # **違反を伝えて書き直させる。** 本文そのものは会話に積まない --
        # 積むと入力が回ごとに膨らみ、13.1 のコスト上限が意味を失う。
        messages = [
            *messages,
            {"role": "assistant", "content": "(前回の本文)"},
            {"role": "user", "content": _violation_note(violations)},
        ]

    return GeneratedMessage(
        candidate_id=candidate.candidate_id,
        outcome=GenerationOutcome.STILL_INVALID,
        body=body,
        violations=violations,
        attempts=attempts,
        usage=total,
    )


def _failure_name(exc: BaseException) -> str:
    """The exception type, plus the original type it wrapped. **文言は残さない。**

    ``call_structured`` は API 由来の例外を :class:`GenerationError` に包む。
    型名だけを残すと、8.2 の事故 (拡張思考のパラメータ形式が廃止され、全件が
    400 で失敗した) が「GenerationError が N件」としか見えない -- **仕様変更なのか
    こちらの不備なのかが報告から決まらない。** 元の型まで残せば切り分けられる。

    **例外の文言は残さない** (13.2)。APIのエラー文言は要求の中身を引用することが
    あり、そこには候補者の情報も本文も入りうる。
    """
    cause = exc.__cause__ or exc.__context__
    if cause is not None and type(cause) is not type(exc):
        return f"{type(exc).__name__}({type(cause).__name__})"
    return type(exc).__name__


def _add(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cache_read_input_tokens=left.cache_read_input_tokens + right.cache_read_input_tokens,
        cache_creation_input_tokens=(
            left.cache_creation_input_tokens + right.cache_creation_input_tokens
        ),
    )
