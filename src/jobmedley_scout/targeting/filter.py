"""Apply every targeting rule to one candidate.

判定は **早期 return しない**。1つ落ちた時点で打ち切ると、レポートに出る除外理由が
「最初に落ちたルール」だけになり、閾値を緩めたときに何が起きるかを誰も見積もれなく
なる。全ルールの outcome を保持して返す。

7.1 の帰結として、判定不能を潰すのは
:func:`~jobmedley_scout.targeting.determination.resolve_undeterminable` 1箇所だけ。
本モジュールはその結果を集約するだけで、独自の畳み込みを持たない。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from jobmedley_scout.config.schema import TargetingConfig
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.targeting.determination import (
    Determination,
    RuleOutcome,
    resolve_undeterminable,
)
from jobmedley_scout.targeting.registry import ALL_RULES, assert_policies_complete
from jobmedley_scout.targeting.rules import RULE_MEMBERSHIP_STATUS, rule_membership_status

_STRICT = ConfigDict(extra="forbid", frozen=True)


class TargetingResult(BaseModel):
    """The verdict for one candidate, with every rule's outcome retained."""

    model_config = _STRICT

    is_target: bool
    outcomes: tuple[RuleOutcome, ...]
    #: 8.3 対策2: 実際に条件を満たした値だけ。生成側はここにある値しか
    #: 「共通点」として書いてはならない。
    matched_values: tuple[str, ...]
    #: 除外理由。**判定不能を方針で除外した場合もここに入る** -- 入れないと
    #: 「なぜ送られなかったのか」が復元できず、7.1 の再発を検知できない。
    rejection_reasons: tuple[str, ...]
    undeterminable_rules: tuple[str, ...]


def apply_targeting(
    candidate: Candidate,
    cfg: TargetingConfig,
    membership_qualifying: tuple[str, ...] | None = None,
) -> TargetingResult:
    """Evaluate every rule and combine the outcomes with the declared policies.

    ``membership_qualifying`` は未確定の媒体座標なので既定は ``None``
    (=判定不能)。呼び出し側が確定値を持っているときだけ渡す。
    """
    # 設定差し替え経路が完全性検査を迂回しないよう、毎回検査する (安価)。
    assert_policies_complete(cfg)

    outcomes: list[RuleOutcome] = []
    matched_values: list[str] = []
    rejection_reasons: list[str] = []
    undeterminable_rules: list[str] = []
    is_target = True

    for rule_id, rule in ALL_RULES:
        if rule_id == RULE_MEMBERSHIP_STATUS:
            # 対象値は設定ではなく座標。ここだけ明示的に渡す。
            outcome = rule_membership_status(candidate, cfg, qualifying=membership_qualifying)
        else:
            outcome = rule(candidate, cfg)
        outcomes.append(outcome)

        policy = cfg.undeterminable_policy[rule_id]
        passed = resolve_undeterminable(outcome, policy)
        if not passed:
            is_target = False
        if outcome.determination is Determination.MATCH:
            for value in outcome.matched_values:
                if value not in matched_values:
                    matched_values.append(value)
        elif outcome.determination is Determination.UNDETERMINABLE:
            undeterminable_rules.append(rule_id)
            if not passed:
                rejection_reasons.append(
                    f"{rule_id}: 判定不能のため除外 (方針={policy.value}) — {outcome.evidence}"
                )
        else:
            rejection_reasons.append(f"{rule_id}: {outcome.evidence}")

    return TargetingResult(
        is_target=is_target,
        outcomes=tuple(outcomes),
        matched_values=tuple(matched_values),
        rejection_reasons=tuple(rejection_reasons),
        undeterminable_rules=tuple(undeterminable_rules),
    )
