"""Three-tier enum mapping.

6.5 の事故:

> 媒体のenum値は公開されておらず、網羅は不可能です。参照実装では、学歴の値が
> マップに無い値で返り、判定不能に落ちて **本来対象の候補者を取りこぼしました。**

3段構え:

1. 既知値の完全一致マップ
2. 一致しなければ、小文字化した文字列に対する **キーワード推定**
3. 真に判別不能な値のみ「不明」とし、**生の値をログに出す** (マッピング追加のサイン)

**どちら側に倒すかを設計時に明示すること。** 学歴は「大学卒以上を取りこぼさない」
ことを最優先にし、上位学歴から順に判定する
(:data:`models.enums.EDUCATION_MATCH_ORDER` -- 「大学院」は「大学」を部分文字列と
して含むので、順序そのものが仕様である)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

from jobmedley_scout.models.enums import EDUCATION_MATCH_ORDER, EducationLevel
from jobmedley_scout.models.text_norm import fold_width


class MappingTier(StrEnum):
    """Which tier produced the answer. Recorded so drift is visible."""

    EXACT = "exact"
    KEYWORD = "keyword"
    UNKNOWN = "unknown"


T = TypeVar("T")


@dataclass(frozen=True)
class EnumMapping(Generic[T]):
    """A mapped value plus how it was obtained."""

    value: T
    tier: MappingTier
    raw: str

    @property
    def needs_attention(self) -> bool:
        """True when the raw value should be added to the exact map.

        キーワード推定で当たった値も報告対象にする -- 推定が効いているうちに
        完全一致マップへ足しておかないと、次の表記揺れで UNKNOWN に落ちる。
        """
        return self.tier is not MappingTier.EXACT


#: 学歴のキーワード推定。**上位学歴から順に** 評価する (EDUCATION_MATCH_ORDER)。
#: 「大学院」を先に見ないと大学院卒が大学卒に畳まれる。
EDUCATION_KEYWORDS: dict[EducationLevel, tuple[str, ...]] = {
    EducationLevel.DOCTORATE: ("博士", "doctor", "phd", "d."),
    EducationLevel.GRADUATE: ("大学院", "修士", "master", "graduate school", "院卒"),
    EducationLevel.TECHNICAL_COLLEGE: ("高専", "工業高等専門", "technical college"),
    EducationLevel.JUNIOR_COLLEGE: ("短大", "短期大学", "junior college"),
    EducationLevel.UNIVERSITY: ("大学", "学士", "university", "bachelor", "大卒"),
    EducationLevel.VOCATIONAL: ("専門学校", "専修", "vocational", "専門課程"),
    EducationLevel.HIGH_SCHOOL: ("高校", "高等学校", "high school", "高卒"),
}


def map_education(
    raw: str | None, exact_map: Mapping[str, str] | None
) -> EnumMapping[EducationLevel]:
    """Map a raw education string onto :class:`EducationLevel`.

    ``exact_map`` は座標 ``enums.education.exact_map`` から来る実測値。未確定なら
    ``None`` で、その場合はキーワード推定だけで判定する。
    """
    if raw is None or not raw.strip():
        return EnumMapping(EducationLevel.UNKNOWN, MappingTier.UNKNOWN, "")

    normalized = fold_width(raw).strip()

    # 第1段: 完全一致。
    if exact_map:
        hit = exact_map.get(normalized)
        if hit is not None:
            try:
                return EnumMapping(EducationLevel(hit), MappingTier.EXACT, normalized)
            except ValueError:
                # マップの値そのものが不正。設定の誤りなので不明に落として報告する。
                return EnumMapping(EducationLevel.UNKNOWN, MappingTier.UNKNOWN, normalized)

    # 第2段: キーワード推定。上位学歴から順に見る (6.5)。
    lowered = normalized.casefold()
    for level in EDUCATION_MATCH_ORDER:
        for keyword in EDUCATION_KEYWORDS[level]:
            if keyword.casefold() in lowered:
                return EnumMapping(level, MappingTier.KEYWORD, normalized)

    # 第3段: 真に判別不能。生値を保持して呼び出し側がログに出す。
    return EnumMapping(EducationLevel.UNKNOWN, MappingTier.UNKNOWN, normalized)


def map_membership(raw: str | None, qualifying: tuple[str, ...] | None) -> EnumMapping[bool | None]:
    """Whether a membership status qualifies.

    対象とする会員ステータスの値は座標 (``enums.membership.qualifying_values``)。
    **未確定なら判定不能 (None) を返す** -- 推測で「たぶんこれが有料会員だろう」と
    判定すると、対象外の候補者へ送るか、対象者を丸ごと落とすかのどちらかになる。
    """
    if qualifying is None:
        return EnumMapping(None, MappingTier.UNKNOWN, raw or "")
    if raw is None or not raw.strip():
        return EnumMapping(None, MappingTier.UNKNOWN, "")

    normalized = fold_width(raw).strip()
    if normalized in qualifying:
        return EnumMapping(True, MappingTier.EXACT, normalized)
    # 完全一致しない値は「対象外」ではなく **判定不能**。会員種別の表記が
    # 変わっただけかもしれず、黙って除外すると 6.5 の取りこぼしを再現する。
    lowered = normalized.casefold()
    for value in qualifying:
        if value.casefold() == lowered:
            return EnumMapping(True, MappingTier.KEYWORD, normalized)
    return EnumMapping(None, MappingTier.UNKNOWN, normalized)


def unknown_values(mappings: tuple[EnumMapping[object], ...]) -> tuple[str, ...]:
    """Raw values that fell through to UNKNOWN.

    **マッピング追加のサイン** (6.5)。実行レポートに出すこと -- ログの奥に
    埋めると、取りこぼしが起きていることに誰も気づかない。
    """
    seen: dict[str, None] = {}
    for mapping in mappings:
        if mapping.tier is MappingTier.UNKNOWN and mapping.raw:
            seen.setdefault(mapping.raw, None)
    return tuple(seen)
