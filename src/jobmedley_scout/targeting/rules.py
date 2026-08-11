"""The targeting rules. Pure predicates over a candidate and the config.

Every rule has the same shape ``(candidate, cfg) -> RuleOutcome`` and every rule
returns three-valued :class:`Determination` -- 7.1。``MATCH`` は常に
「この候補者はこのルールを満たす」であり、除外系のルール (外国語ネイティブ・
海外大学) は該当した場合に ``NO_MATCH`` を返す。呼び出し側が
「どっち向きのルールだったか」を覚えなくてよいようにするためである。

rule_id は ``config/config.yaml`` の ``targeting.undeterminable_policy`` のキーと
一致していなければならない。一致の検査は
:func:`jobmedley_scout.targeting.registry.assert_policies_complete`。
"""

from __future__ import annotations

from typing import Final

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.config.schema import TargetingConfig
from jobmedley_scout.models.candidate import Candidate, Education
from jobmedley_scout.models.enums import EDUCATION_MATCH_ORDER, EDUCATION_RANK, EducationLevel
from jobmedley_scout.models.enums import meets_minimum_education
from jobmedley_scout.models.text_norm import normalize_identifier
from jobmedley_scout.targeting.dedupe import count_job_changes, dedupe_employers
from jobmedley_scout.targeting.determination import (
    Determination,
    RuleOutcome,
    matched,
    not_matched,
    undeterminable,
)
from jobmedley_scout.targeting.language import detect_foreign_native_detail
from jobmedley_scout.targeting.university import classify_university

RULE_AGE: Final = "age"
RULE_LONGEST_TENURE: Final = "longest_tenure"
RULE_CURRENT_TENURE: Final = "current_tenure"
RULE_JOB_CHANGE_COUNT: Final = "job_change_count"
RULE_EDUCATION: Final = "education"
RULE_MEMBERSHIP_STATUS: Final = "membership_status"
RULE_FOREIGN_NATIVE: Final = "foreign_native"
RULE_DOMESTIC_UNIVERSITY: Final = "domestic_university"

#: 生値 -> 学歴 enum のキーワード表。**判定順は
#: :data:`EDUCATION_MATCH_ORDER` に従う** (上位学歴から)。「大学院」は「大学」を
#: 部分文字列として含むので、順序を崩すと大学院卒が大学卒に畳まれる (6.5)。
#: これは写像が確定するまでの最小限の実装である。媒体の実値を観測したら
#: ``api.enums_map`` の3段構えへ委譲し、ここは薄い転送にすること。
EDUCATION_KEYWORDS: dict[EducationLevel, tuple[str, ...]] = {
    EducationLevel.DOCTORATE: ("博士", "doctor", "phd", "ph.d"),
    EducationLevel.GRADUATE: ("大学院", "修士", "master", "mba"),
    EducationLevel.TECHNICAL_COLLEGE: ("高専", "高等専門学校"),
    EducationLevel.JUNIOR_COLLEGE: ("短大", "短期大学", "junior college"),
    EducationLevel.UNIVERSITY: ("大学", "学士", "bachelor", "university"),
    EducationLevel.VOCATIONAL: ("専門学校", "専修学校", "vocational"),
    EducationLevel.HIGH_SCHOOL: ("高校", "高等学校", "高卒", "high school"),
}


def map_education_level(raw_level: str | None) -> EducationLevel:
    """Map a raw education string to :class:`EducationLevel`.

    Exact enum value first, then keywords in :data:`EDUCATION_MATCH_ORDER`.
    判別できなければ ``UNKNOWN``。**推測で近い学歴に寄せない** (6.5: マップに無い
    値を黙って畳んだ結果、本来対象の候補者を取りこぼした)。
    """
    if raw_level is None:
        return EducationLevel.UNKNOWN
    normalized = normalize_identifier(raw_level)
    if not normalized:
        return EducationLevel.UNKNOWN
    for level in EducationLevel:
        if normalized == level.value:
            return level
    for level in EDUCATION_MATCH_ORDER:
        for keyword in EDUCATION_KEYWORDS[level]:
            if normalize_identifier(keyword) in normalized:
                return level
    return EducationLevel.UNKNOWN


def rule_age(candidate: Candidate, cfg: TargetingConfig) -> RuleOutcome:
    """Age within ``[age_min, age_max]``, both inclusive."""
    age = candidate.resume.age
    if age is None:
        return undeterminable(RULE_AGE, evidence="年齢が未取得 (キー写像が未確定の可能性)")
    if cfg.age_min <= age <= cfg.age_max:
        return matched(
            RULE_AGE,
            evidence=f"年齢{age}歳は対象範囲 {cfg.age_min}〜{cfg.age_max}歳の内",
            matched_values=(f"{age}歳",),
        )
    return not_matched(
        RULE_AGE, evidence=f"年齢{age}歳は対象範囲 {cfg.age_min}〜{cfg.age_max}歳の外"
    )


def rule_longest_tenure(candidate: Candidate, cfg: TargetingConfig) -> RuleOutcome:
    """Longest tenure at a single employer clears ``min_longest_tenure_years``."""
    employments = candidate.resume.employments
    if not employments:
        return undeterminable(RULE_LONGEST_TENURE, evidence="職歴が未取得")
    threshold = cfg.min_longest_tenure_years
    qualifying = tuple(
        employment
        for employment in employments
        if employment.tenure_years is not None and employment.tenure_years >= threshold
    )
    if qualifying:
        # 8.3 対策2: 実際に閾値を超えた勤務先 **だけ** を提示値にする。
        # 参照実装は全勤務先を下流へ渡し、モデルが複数社について「同じ◯◯」と書いた。
        values = tuple(e.company for e in qualifying if e.company)
        longest = max(e.tenure_years or 0.0 for e in qualifying)
        return matched(
            RULE_LONGEST_TENURE,
            evidence=f"最長勤続{longest}年 >= {threshold}年 (該当{len(qualifying)}社)",
            matched_values=values,
        )
    known = tuple(e for e in employments if e.tenure_years is not None)
    if not known:
        return undeterminable(RULE_LONGEST_TENURE, evidence="在籍年数が1件も取得できていない")
    if len(known) < len(employments):
        # 既知の最大が閾値未満でも、未知の職歴が閾値を超えている可能性が残る。
        # ここを NO_MATCH に畳むと「取れなかった」が「満たさない」に化ける (7.1)。
        return undeterminable(
            RULE_LONGEST_TENURE,
            evidence=(
                f"既知の最長勤続{max(e.tenure_years or 0.0 for e in known)}年は{threshold}年未満だが、"
                f"在籍年数不明の職歴が{len(employments) - len(known)}件ある"
            ),
        )
    return not_matched(
        RULE_LONGEST_TENURE,
        evidence=f"最長勤続{max(e.tenure_years or 0.0 for e in known)}年 < {threshold}年",
    )


def rule_current_tenure(candidate: Candidate, cfg: TargetingConfig) -> RuleOutcome:
    """Current employment tenure clears ``min_current_tenure_years``.

    7.1 の実事故はここである。「直近1年以内に転職した候補者を除外する」を足した
    とき、**現職の在籍年数が取れていない候補者がすり抜けた。** 最長勤続年数の
    ルールが過去の職歴データで合格を出し、その合格が「現職が不明」を覆い隠した。
    現職が無い / 年数が無い場合は必ず UNDETERMINABLE を返し、設定の方針
    (現状 ``exclude``) に倒させる。ここで False や True に畳まないこと。
    """
    current = candidate.resume.current_employment()
    if current is None:
        return undeterminable(
            RULE_CURRENT_TENURE, evidence="現職が特定できない (is_current の職歴が無い)"
        )
    if current.tenure_years is None:
        return undeterminable(RULE_CURRENT_TENURE, evidence="現職の在籍年数が未取得")
    threshold = cfg.min_current_tenure_years
    if current.tenure_years >= threshold:
        return matched(
            RULE_CURRENT_TENURE,
            evidence=f"現職の在籍{current.tenure_years}年 >= {threshold}年",
            matched_values=(current.company,) if current.company else (),
        )
    return not_matched(
        RULE_CURRENT_TENURE,
        evidence=f"現職の在籍{current.tenure_years}年 < {threshold}年 (直近の転職)",
    )


def job_change_threshold(age: int, cfg: TargetingConfig) -> int:
    """Exclusion threshold for the age band. ``count >= threshold`` excludes."""
    if age <= 29:
        return cfg.job_change_threshold_under_30
    if age <= 39:
        return cfg.job_change_threshold_30s
    return cfg.job_change_threshold_40_plus


def rule_job_change_count(candidate: Candidate, cfg: TargetingConfig) -> RuleOutcome:
    """Job changes below the age-band threshold."""
    age = candidate.resume.age
    if age is None:
        # 年齢帯が決まらなければ閾値も決まらない。既定の帯へ寄せない。
        return undeterminable(RULE_JOB_CHANGE_COUNT, evidence="年齢が未取得のため閾値を選べない")
    employments = candidate.resume.employments
    if not employments:
        return undeterminable(RULE_JOB_CHANGE_COUNT, evidence="職歴が未取得")
    if any(not (employment.company or "").strip() for employment in employments):
        # 社名が無い職歴は重複排除できない。数えれば多すぎ、落とせば少なすぎになる。
        # どちらに倒すかを黙って選ばず、方針に委ねる (7.1)。
        return undeterminable(
            RULE_JOB_CHANGE_COUNT, evidence="社名が未取得の職歴があり重複排除できない"
        )
    current = candidate.resume.current_employment()
    # 7.5: 現職は「現職欄」と「職歴一覧」の両方に現れる。必ず重複排除を通す。
    employers = dedupe_employers(
        current.company if current is not None else None,
        [employment.company for employment in employments],
    )
    count = count_job_changes(
        current.company if current is not None else None,
        [employment.company for employment in employments],
    )
    threshold = job_change_threshold(age, cfg)
    if count >= threshold:
        return not_matched(
            RULE_JOB_CHANGE_COUNT,
            evidence=(
                f"転職回数{count}回 >= 閾値{threshold}回 "
                f"({age}歳・重複排除後{len(employers)}社)"
            ),
        )
    return matched(
        RULE_JOB_CHANGE_COUNT,
        evidence=(f"転職回数{count}回 < 閾値{threshold}回 ({age}歳・重複排除後{len(employers)}社)"),
    )


def _minimum_education(cfg: TargetingConfig) -> EducationLevel:
    try:
        minimum = EducationLevel(cfg.minimum_education)
    except ValueError as exc:
        allowed = ", ".join(level.value for level in EducationLevel)
        raise ConfigError(
            f"targeting.minimum_education の値 '{cfg.minimum_education}' は未知です。"
            f" 指定できるのは: {allowed}"
        ) from exc
    if minimum is EducationLevel.UNKNOWN:
        # 7.6: 寛容に受けると「最低学歴なし」と区別がつかなくなる。
        raise ConfigError("targeting.minimum_education に 'unknown' は指定できません")
    return minimum


def rule_education(candidate: Candidate, cfg: TargetingConfig) -> RuleOutcome:
    """Highest known education level meets ``minimum_education``.

    設定の方針はこのルールだけ ``include`` である (6.5:
    **大学卒以上を取りこぼさない**)。非対称な業務判断が YAML の diff に見える。
    """
    minimum = _minimum_education(cfg)
    educations = candidate.resume.educations
    if not educations:
        return undeterminable(RULE_EDUCATION, evidence="学歴が未取得")
    levels: list[tuple[Education, EducationLevel]] = [
        (education, map_education_level(education.raw_level)) for education in educations
    ]
    known = [(e, lv) for e, lv in levels if lv is not EducationLevel.UNKNOWN]
    if not known:
        raw = ", ".join(f"'{e.raw_level}'" for e in educations if e.raw_level) or "(生値なし)"
        # 6.5: 判別不能なら生値を残す。マップに無い値が来ていることが読み取れないと、
        # 「取りこぼしているのに誰も気づかない」状態になる。
        return undeterminable(RULE_EDUCATION, evidence=f"学歴の生値を判別できない: {raw}")
    best_level = max((lv for _, lv in known), key=lambda level: EDUCATION_RANK[level])
    verdict = meets_minimum_education(best_level, minimum)
    if verdict is None:  # pragma: no cover - known は UNKNOWN を含まない
        return undeterminable(RULE_EDUCATION, evidence="学歴を比較できない")
    if verdict:
        qualifying = tuple(
            e.school for e, lv in known if e.school and meets_minimum_education(lv, minimum) is True
        )
        return matched(
            RULE_EDUCATION,
            evidence=f"最終学歴 {best_level.value} >= {minimum.value}",
            matched_values=qualifying,
        )
    return not_matched(RULE_EDUCATION, evidence=f"最終学歴 {best_level.value} < {minimum.value}")


def rule_membership_status(
    candidate: Candidate,
    cfg: TargetingConfig,
    *,
    qualifying: tuple[str, ...] | None = None,
) -> RuleOutcome:
    """Membership status is one of the qualifying values.

    ``qualifying`` は **媒体座標であり、まだ確定していない**。実値を観測して
    いない以上、コードにも設定にも列挙しない (原則3・6.4)。``None`` のまま
    呼ばれたら UNDETERMINABLE を返す -- 推測した値と比較して「不一致だから除外」と
    するのが最悪の振る舞いであり、既定値を置かないことでそれを構造的に防ぐ。
    """
    del cfg  # 会員ステータスの対象値は設定ではなく座標側にある。
    if qualifying is None:
        return undeterminable(
            RULE_MEMBERSHIP_STATUS,
            evidence="対象となる会員ステータスの値が未確定 (媒体座標)",
        )
    status = candidate.resume.membership_status
    if status is None or not status.strip():
        return undeterminable(RULE_MEMBERSHIP_STATUS, evidence="会員ステータスが未取得")
    normalized = normalize_identifier(status)
    wanted = {normalize_identifier(value) for value in qualifying}
    if normalized in wanted:
        return matched(
            RULE_MEMBERSHIP_STATUS,
            evidence=f"会員ステータス '{status}' は対象",
            matched_values=(status,),
        )
    return not_matched(RULE_MEMBERSHIP_STATUS, evidence=f"会員ステータス '{status}' は対象外")


def rule_foreign_native(candidate: Candidate, cfg: TargetingConfig) -> RuleOutcome:
    """Not a foreign-language native speaker.

    7.2: 判定材料は **語学欄だけ**。``resume.summary`` や職歴本文をここへ渡しては
    ならない (「ネイティブ広告」「クラウドネイティブ」で日本人を除外しかけた)。
    """
    detection = detect_foreign_native_detail(candidate.resume.language_text, cfg.foreign_language)
    if detection.determination is Determination.UNDETERMINABLE:
        return undeterminable(RULE_FOREIGN_NATIVE, evidence=detection.evidence)
    if detection.determination is Determination.MATCH:
        # 外国語ネイティブに該当 = 対象外。ルールの MATCH 向きは常に「対象である」。
        return not_matched(RULE_FOREIGN_NATIVE, evidence=detection.evidence)
    return matched(RULE_FOREIGN_NATIVE, evidence=detection.evidence)


def rule_domestic_university(candidate: Candidate, cfg: TargetingConfig) -> RuleOutcome:
    """No overseas university in the education history."""
    educations = candidate.resume.educations
    if not educations:
        return undeterminable(RULE_DOMESTIC_UNIVERSITY, evidence="学歴が未取得")
    allowlist = cfg.domestic_katakana_universities
    classifications = [
        (education, classify_university(education.school, allowlist)) for education in educations
    ]
    overseas = [(e, c) for e, c in classifications if c.determination is Determination.MATCH]
    if overseas:
        schools = "、".join(f"「{c.core}」" for _, c in overseas)
        return not_matched(RULE_DOMESTIC_UNIVERSITY, evidence=f"海外の学校と判定: {schools}")
    domestic = [(e, c) for e, c in classifications if c.determination is Determination.NO_MATCH]
    if not domestic:
        return undeterminable(
            RULE_DOMESTIC_UNIVERSITY,
            evidence="; ".join(c.evidence for _, c in classifications),
        )
    return matched(
        RULE_DOMESTIC_UNIVERSITY,
        evidence="; ".join(c.evidence for _, c in domestic),
        matched_values=tuple(e.school for e, _ in domestic if e.school),
    )
