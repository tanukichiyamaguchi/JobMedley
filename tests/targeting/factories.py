"""Builders for targeting tests.

設定は ``config/config.yaml`` の実値を写しているが、**ファイルを読まない**。
テストが実運用の設定変更で赤くなると、判定ロジックの回帰と設定の変更が
区別できなくなるため。実ファイルとの整合は test_registry.py が別途検査する。
"""

from __future__ import annotations

from typing import Any

from jobmedley_scout.config.schema import (
    ForeignLanguageConfig,
    TargetingConfig,
    UndeterminablePolicy,
)
from jobmedley_scout.models.candidate import Candidate, Education, Employment, ResumeFacts

DEFAULT_POLICIES: dict[str, UndeterminablePolicy] = {
    "age": UndeterminablePolicy.EXCLUDE,
    "longest_tenure": UndeterminablePolicy.EXCLUDE,
    "current_tenure": UndeterminablePolicy.EXCLUDE,
    "job_change_count": UndeterminablePolicy.EXCLUDE,
    "education": UndeterminablePolicy.INCLUDE,
    "membership_status": UndeterminablePolicy.EXCLUDE,
    "foreign_native": UndeterminablePolicy.INCLUDE,
    "domestic_university": UndeterminablePolicy.INCLUDE,
}

QUALIFYING_MEMBERSHIP: tuple[str, ...] = ("スカウト受付中",)


def make_foreign_language_config(**overrides: Any) -> ForeignLanguageConfig:
    values: dict[str, Any] = {
        "proximity_max_distance": 10,
        "latin_min_chars": 50,
        "japanese_ratio_threshold": 0.15,
        "foreign_languages": ("英語", "中国語", "韓国語", "ポルトガル語", "ネパール語"),
        "japanese_tokens": ("日本語", "国語", "Japanese"),
        "native_markers": ("ネイティブ", "ネイティヴ", "native", "母語", "母国語"),
    }
    values.update(overrides)
    return ForeignLanguageConfig(**values)


def make_targeting_config(**overrides: Any) -> TargetingConfig:
    values: dict[str, Any] = {
        "age_min": 27,
        "age_max": 42,
        "min_longest_tenure_years": 2.5,
        "min_current_tenure_years": 1.0,
        "job_change_threshold_under_30": 3,
        "job_change_threshold_30s": 5,
        "job_change_threshold_40_plus": 6,
        "minimum_education": "university",
        "undeterminable_policy": dict(DEFAULT_POLICIES),
        "foreign_language": make_foreign_language_config(),
        "domestic_katakana_universities": (
            "ノートルダム清心女子大学",
            "フェリス女学院大学",
            "サイバー大学",
            "デジタルハリウッド大学",
        ),
    }
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
    fields: dict[str, Any] = {
        "age": 35,
        "employments": (
            Employment(company="株式会社エー", occupation="営業", tenure_years=8.0),
            Employment(
                company="株式会社ビー", occupation="営業", tenure_years=3.0, is_current=True
            ),
        ),
        "educations": (Education(school="東京大学", raw_level="大学卒", faculty="法学部"),),
        "membership_status": "スカウト受付中",
        "language_text": "日本語：ネイティブ、英語：ビジネスレベル",
    }
    fields.update(resume_overrides)
    return make_candidate(**fields)
