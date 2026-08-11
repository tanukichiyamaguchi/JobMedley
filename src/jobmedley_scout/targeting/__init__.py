"""Targeting: pure, three-valued candidate filtering.

**このパッケージのモジュールはすべて純粋関数である。** ファイルI/O・ネットワーク・
``datetime.now()``・``time.sleep`` を含めないこと (ソース走査のガードレールテストが
これを検査する)。判定の再現に必要なものは全部引数で受け取る。

入口は :func:`apply_targeting`。個々のルールは
:mod:`jobmedley_scout.targeting.rules`、判定不能の扱いは
:mod:`jobmedley_scout.targeting.determination` を参照。
"""

from __future__ import annotations

from jobmedley_scout.targeting.determination import (
    Determination,
    RuleOutcome,
    resolve_undeterminable,
)
from jobmedley_scout.targeting.filter import TargetingResult, apply_targeting
from jobmedley_scout.targeting.registry import (
    ALL_RULE_IDS,
    ALL_RULES,
    Rule,
    assert_policies_complete,
)

__all__ = [
    "ALL_RULES",
    "ALL_RULE_IDS",
    "Determination",
    "Rule",
    "RuleOutcome",
    "TargetingResult",
    "apply_targeting",
    "assert_policies_complete",
    "resolve_undeterminable",
]
