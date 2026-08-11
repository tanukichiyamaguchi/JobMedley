"""Builders shared by the generation tests.

設定モデルには既定値が無い (7.6) ため、テストごとに全キーを書くと本質が
埋もれる。ここで既定を1箇所に集約し、各テストは検査したいキーだけを上書きする。
"""

from __future__ import annotations

from datetime import UTC, datetime

from jobmedley_scout.clock import FixedClock
from jobmedley_scout.config.schema import GenerationConfig, MatchingConfig
from jobmedley_scout.generation.assemble import AssemblyContext
from jobmedley_scout.generation.subject import subject_keys
from jobmedley_scout.models.candidate import Candidate, Education, Employment, ResumeFacts
from jobmedley_scout.models.message import GeneratedCore, Introduction

#: 2026-08-11 12:00 JST。件名の日付タグが "8/11" になる。
FIXED_INSTANT = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
PREFIX_LEN = 35


def make_clock() -> FixedClock:
    return FixedClock(FIXED_INSTANT)


def make_matching_config(
    *,
    bidirectional_substring: bool = False,
    min_token_length_for_suffix_match: int = 3,
    industry_generic_terms: tuple[str, ...] = ("サービス業", "製造業", "医療", "介護"),
    job_title_stopwords: tuple[str, ...] = ("課長", "部長", "主任"),
) -> MatchingConfig:
    return MatchingConfig(
        bidirectional_substring=bidirectional_substring,
        min_token_length_for_suffix_match=min_token_length_for_suffix_match,
        industry_generic_terms=industry_generic_terms,
        job_title_stopwords=job_title_stopwords,
    )


def make_generation_config(
    *,
    max_introductions: int = 3,
    min_introductions: int = 1,
    followup_introductions: int = 1,
    max_exclamation_marks: int = 2,
    assertive_terms: tuple[str, ...] = (
        "同じ",
        "同様に",
        "同期",
        "同じく",
        "共通点",
        "同郷",
        "同窓",
    ),
    allowed_soft_terms: tuple[str, ...] = ("近い", "近しい"),
    url_allowlist: tuple[str, ...] = ("job-medley.com",),
    matching: MatchingConfig | None = None,
) -> GenerationConfig:
    return GenerationConfig(
        max_introductions=max_introductions,
        min_introductions=min_introductions,
        followup_introductions=followup_introductions,
        max_exclamation_marks=max_exclamation_marks,
        assertive_terms=assertive_terms,
        allowed_soft_terms=allowed_soft_terms,
        url_allowlist=url_allowlist,
        matching=matching if matching is not None else make_matching_config(),
    )


def make_candidate(
    *,
    candidate_id: str = "10001",
    display_name: str = "田中太郎",
    resume: ResumeFacts | None = None,
) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        raw_id_observed=candidate_id,
        display_name=display_name,
        resume=resume if resume is not None else ResumeFacts(),
    )


def make_resume(
    *,
    current_company: str | None = None,
    current_occupation: str | None = None,
    previous_company: str | None = None,
    previous_occupation: str | None = None,
    experienced_industries: tuple[str, ...] = (),
    experienced_occupations: tuple[str, ...] = (),
    school: str | None = None,
    desired_locations: tuple[str, ...] = (),
    language_text: str | None = None,
) -> ResumeFacts:
    employments: list[Employment] = []
    if current_company is not None or current_occupation is not None:
        employments.append(
            Employment(
                company=current_company,
                occupation=current_occupation,
                tenure_years=3.0,
                is_current=True,
            )
        )
    if previous_company is not None or previous_occupation is not None:
        employments.append(
            Employment(
                company=previous_company,
                occupation=previous_occupation,
                is_current=False,
            )
        )
    educations = (
        (Education(school=school, raw_level="大学卒", faculty="経済学部"),) if school else ()
    )
    return ResumeFacts(
        employments=tuple(employments),
        educations=educations,
        experienced_industries=experienced_industries,
        experienced_occupations=experienced_occupations,
        desired_locations=desired_locations,
        language_text=language_text,
    )


def make_core(
    *,
    subject: str = "介護のお仕事のご紹介",
    opening: str = "はじめまして、ジョブメドレー担当の佐藤と申します。",
    motivation: str = "ご経歴を拝見し、ぜひ一度お話ししたくご連絡いたしました。",
    introductions: tuple[Introduction, ...] | None = None,
    closing: str = "ご返信をお待ちしております。",
) -> GeneratedCore:
    if introductions is None:
        introductions = (
            Introduction(person_id="p-001", blurb="鈴木は介護施設の立ち上げを担当しています。"),
        )
    return GeneratedCore(
        subject=subject,
        opening=opening,
        motivation=motivation,
        introductions=introductions,
        closing=closing,
    )


def make_context(
    *,
    recipient_name: str = "田中太郎",
    subject: str = "田中太郎様｜介護のお仕事のご紹介｜8/11",
    notes: tuple[str, ...] = ("本メールはご返信いただければ担当者に届きます。",),
    boilerplate: tuple[str, ...] = ("配信停止をご希望の場合はその旨ご返信ください。",),
    signature_lines: tuple[str, ...] = ("株式会社サンプル 採用支援チーム", "佐藤 花子"),
    footer_lines: tuple[str, ...] = ("〒100-0001 東京都千代田区1-1-1",),
    footer_url: str | None = "https://job-medley.com/scout/",
) -> AssemblyContext:
    return AssemblyContext(
        recipient_name=recipient_name,
        subject=subject_keys(subject, PREFIX_LEN),
        notes=notes,
        boilerplate=boilerplate,
        signature_lines=signature_lines,
        footer_lines=footer_lines,
        footer_url=footer_url,
    )
