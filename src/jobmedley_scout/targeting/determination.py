"""Three-valued rule outcomes.

7.1: **「スキップは、黙って合格させるのと同義」。** ルールは決して ``bool`` を
返さない。参照実装の事故は次の形だった:

「直近1年以内に転職した候補者を除外する」を足したところ、**現職の在籍年数が
取得できていない候補者がすり抜けた。** 別のルール (最長勤続年数) が過去の職歴
データで合格を出しており、その合格が「現職が不明である」事実を覆い隠したからで
ある。判定不能を ``False``/``True`` のどちらかに畳んだ瞬間、その情報は失われ、
どのルールが何を根拠に通したのかを後から誰も再構成できない。

したがって本モジュールの型は3値であり、**判定不能を潰すのは
:func:`resolve_undeterminable` ただ1箇所** に限定する。そこでは必ず設定由来の
:class:`UndeterminablePolicy` を要求するので、「どちらに倒したか」は YAML の
diff に現れる (既定値は無い)。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from jobmedley_scout.config.schema import UndeterminablePolicy

_STRICT = ConfigDict(extra="forbid", frozen=True)


class Determination(StrEnum):
    """The verdict of a single targeting rule.

    ``MATCH`` always means "this candidate satisfies the rule", never "the
    condition described by the rule name is true" -- 除外系のルール
    (外国語ネイティブなど) は、該当した場合に ``NO_MATCH`` を返す。
    """

    MATCH = "match"
    NO_MATCH = "no_match"
    UNDETERMINABLE = "undeterminable"


class RuleOutcome(BaseModel):
    """One rule's verdict, plus the evidence a human needs to audit it."""

    model_config = _STRICT

    rule_id: str
    determination: Determination
    #: 8.3 対策2: **判定に使った値と提示する値を一致させる。** 参照実装は
    #: 候補者の勤務先を *全部* 下流へ渡しており、実際に一致したのは1社だけなのに
    #: モデルが複数社について「同じ◯◯」と書いた。ここには
    #: 「実際に条件を満たした値」だけを入れる。
    matched_values: tuple[str, ...] = ()
    evidence: str

    @model_validator(mode="after")
    def _matched_values_require_match(self) -> RuleOutcome:
        # 8.3 対策2 を型ではなく構造で担保する。不一致・判定不能の outcome に
        # 値が載っていたら、それは下流で「一致した根拠」として使われうる。
        # 根拠にならない値は evidence (自由文) 側へ書くこと。
        if self.determination is not Determination.MATCH and self.matched_values:
            raise ValueError(
                f"rule_id={self.rule_id}: determination={self.determination} に "
                f"matched_values は載せられません (一致した値だけを載せる)"
            )
        return self


def matched(rule_id: str, *, evidence: str, matched_values: tuple[str, ...] = ()) -> RuleOutcome:
    """Build a ``MATCH`` outcome."""
    return RuleOutcome(
        rule_id=rule_id,
        determination=Determination.MATCH,
        matched_values=matched_values,
        evidence=evidence,
    )


def not_matched(rule_id: str, *, evidence: str) -> RuleOutcome:
    """Build a ``NO_MATCH`` outcome."""
    return RuleOutcome(rule_id=rule_id, determination=Determination.NO_MATCH, evidence=evidence)


def undeterminable(rule_id: str, *, evidence: str) -> RuleOutcome:
    """Build an ``UNDETERMINABLE`` outcome.

    ここを返すのは恥ではなく仕様である。値が取れていない・写像が未確定である
    ことを、合否のどちらかに畳まずそのまま上へ渡す。
    """
    return RuleOutcome(
        rule_id=rule_id, determination=Determination.UNDETERMINABLE, evidence=evidence
    )


def resolve_undeterminable(outcome: RuleOutcome, policy: UndeterminablePolicy) -> bool:
    """Collapse an outcome into pass/fail, applying ``policy`` only when undecided.

    **判定不能を潰してよい唯一の場所。** ここ以外で ``Determination`` を真偽値に
    変換しないこと (7.1)。方針は設定必須項目なので、呼び出し側は「何も指定しない」
    という選択ができない。
    """
    if outcome.determination is Determination.MATCH:
        return True
    if outcome.determination is Determination.NO_MATCH:
        return False
    return policy is UndeterminablePolicy.INCLUDE
