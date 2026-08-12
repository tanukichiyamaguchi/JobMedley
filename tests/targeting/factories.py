"""Builders for targeting tests.

設定は ``config/config.yaml`` の実値を写しているが、**ファイルを読まない**。
テストが実運用の設定変更で赤くなると、判定ロジックの回帰と設定の変更が
区別できなくなるため。実ファイルとの整合は test_registry.py が別途検査する。
"""

from __future__ import annotations

from typing import Any

from jobmedley_scout.config.schema import TargetingConfig, UndeterminablePolicy
from jobmedley_scout.models.candidate import Candidate, ResumeFacts

#: 走っているルールの全部。**ビズリーチ由来の6ルールは 2026-08-12 に全廃した**
#: (経緯は src/jobmedley_scout/targeting/rules.py の冒頭)。
DEFAULT_POLICIES: dict[str, UndeterminablePolicy] = {
    "membership_status": UndeterminablePolicy.EXCLUDE,
}

QUALIFYING_MEMBERSHIP: tuple[str, ...] = ("スカウト受付中",)


def make_targeting_config(**overrides: Any) -> TargetingConfig:
    values: dict[str, Any] = {"undeterminable_policy": dict(DEFAULT_POLICIES)}
    values.update(overrides)
    return TargetingConfig(**values)


def make_candidate(**resume_fields: Any) -> Candidate:
    """A candidate whose resume carries only the fields given.

    既定は **全項目未取得** (6.4: 写像が確定するまで空のまま)。これがそのまま
    「全ルールが判定不能になるか」のテスト入力になる。
    """
    return Candidate(
        candidate_id="c-1",
        raw_id_observed="c-1",
        display_name="候補 太郎",
        resume=ResumeFacts(**resume_fields),
    )


def make_passing_candidate(**resume_overrides: Any) -> Candidate:
    """A candidate that satisfies every rule, for isolating one rule at a time."""
    fields: dict[str, Any] = {"membership_status": "スカウト受付中"}
    fields.update(resume_overrides)
    return make_candidate(**fields)
