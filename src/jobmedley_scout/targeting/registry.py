"""The rule registry and the policy completeness check.

7.1 の要求は「判定不能時にどちらへ倒すかを **必ず宣言させる**」ことである。
既定値を置けば宣言は省略でき、省略された瞬間に「黙って合格」が戻ってくる。
そこで既定値を置かず、**宣言漏れを起動時の例外にする** ことで構造的に強制する
(:func:`assert_policies_complete`)。

宣言側 (``config/config.yaml``) と実装側 (:data:`ALL_RULES`) は独立に編集される
ので、片側だけの変更を両方向で検知する -- 未宣言のルールも、実装の無い宣言も
エラーにする。後者は打鍵ミスであり、放置すると「設定したつもりのルールが
存在しない」状態になる (7.6)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from jobmedley_scout.config.schema import TargetingConfig
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.targeting.determination import RuleOutcome
from jobmedley_scout.targeting.rules import (
    RULE_MEMBERSHIP_STATUS,
    rule_membership_status,
)

#: 3.11 なので ``type X = ...`` は使えない (TypeAlias で書く)。
Rule: TypeAlias = Callable[[Candidate, TargetingConfig], RuleOutcome]

#: 走っているルールの全部。**ここに並んでいない条件は存在しない。**
#: 2026-08-12 にビズリーチ由来の6ルールを全廃した経緯は rules.py の冒頭にある。
ALL_RULES: tuple[tuple[str, Rule], ...] = ((RULE_MEMBERSHIP_STATUS, rule_membership_status),)

ALL_RULE_IDS: tuple[str, ...] = tuple(rule_id for rule_id, _ in ALL_RULES)


def assert_policies_complete(cfg: TargetingConfig) -> None:
    """Raise :class:`ConfigError` unless every rule has a declared policy.

    Called at start-up *and* on every :func:`~jobmedley_scout.targeting.filter.
    apply_targeting` -- 設定を差し替えた経路が検査を迂回できないようにするため。
    """
    declared = set(cfg.undeterminable_policy)
    known = set(ALL_RULE_IDS)
    missing = sorted(known - declared)
    unknown = sorted(declared - known)
    if not missing and not unknown:
        return
    problems: list[str] = []
    if missing:
        problems.append(
            "targeting.undeterminable_policy に方針の宣言が無いルール: "
            + ", ".join(missing)
            + " (7.1: 既定値は置かない。include / exclude を明記すること)"
        )
    if unknown:
        problems.append(
            "targeting.undeterminable_policy に実装の無いルールIDがあります: "
            + ", ".join(unknown)
            + " (打鍵ミスの可能性。宣言したつもりのルールが効かない状態になる)"
        )
    raise ConfigError("\n".join(problems))
