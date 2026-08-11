"""Platform enumerations.

6.5 の要点: 媒体のenum値は公開されておらず、網羅は不可能。参照実装では学歴の値が
マップに無い値で返り、判定不能に落ちて **本来対象の候補者を取りこぼした。**

したがって:

* すべての enum が明示的な ``UNKNOWN`` メンバを持つ (一級市民として扱う)。
* 生値→enum の写像は :mod:`jobmedley_scout.api.enums_map` の3段構えで行う。
* **どちら側に倒すかを設計時に明示する** -- 学歴は「大学卒以上を取りこぼさない」
  ことを最優先とし、上位学歴から順に判定する (:data:`EDUCATION_MATCH_ORDER`)。

会員ステータスは enum にしていない。ジョブメドレーでの実値を観測していないため
であり、推測で列挙すると「マップに無い値が来て判定不能」という 6.5 の事故を
自分で作り込むことになる。正規化済みの生文字列として持ち、どの値が対象かは
座標 (設定) 側で宣言する。
"""

from __future__ import annotations

from enum import StrEnum


class EducationLevel(StrEnum):
    """Highest education level attained."""

    UNKNOWN = "unknown"
    HIGH_SCHOOL = "high_school"
    VOCATIONAL = "vocational"
    JUNIOR_COLLEGE = "junior_college"
    TECHNICAL_COLLEGE = "technical_college"
    UNIVERSITY = "university"
    GRADUATE = "graduate"
    DOCTORATE = "doctorate"


#: 序列。``UNKNOWN`` は意図的に含めない -- 順序を持たせると「不明は高卒相当」の
#: ような暗黙の推測が入り込む。不明は不明として別扱いにする。
EDUCATION_RANK: dict[EducationLevel, int] = {
    EducationLevel.HIGH_SCHOOL: 1,
    EducationLevel.VOCATIONAL: 2,
    EducationLevel.JUNIOR_COLLEGE: 3,
    EducationLevel.TECHNICAL_COLLEGE: 4,
    EducationLevel.UNIVERSITY: 5,
    EducationLevel.GRADUATE: 6,
    EducationLevel.DOCTORATE: 7,
}

#: キーワード推定を試す順序。**上位学歴から順に判定する** (6.5)。
#: 「大学院」は「大学」を部分文字列として含むため、先に大学院を判定しないと
#: 大学院卒が大学卒として畳まれる。順序自体が仕様である。
EDUCATION_MATCH_ORDER: tuple[EducationLevel, ...] = (
    EducationLevel.DOCTORATE,
    EducationLevel.GRADUATE,
    EducationLevel.TECHNICAL_COLLEGE,
    EducationLevel.JUNIOR_COLLEGE,
    EducationLevel.UNIVERSITY,
    EducationLevel.VOCATIONAL,
    EducationLevel.HIGH_SCHOOL,
)


def meets_minimum_education(level: EducationLevel, minimum: EducationLevel) -> bool | None:
    """Whether ``level`` is at least ``minimum``.

    Returns ``None`` when either side is ``UNKNOWN`` -- 判定不能を ``False`` に
    畳まないこと。7.1 の通り、判定不能を黙って合格/不合格に倒すのが最大の
    抜け穴であり、呼び出し側に ``Determination.UNDETERMINABLE`` として
    明示的に扱わせる。
    """
    if level is EducationLevel.UNKNOWN or minimum is EducationLevel.UNKNOWN:
        return None
    return EDUCATION_RANK[level] >= EDUCATION_RANK[minimum]
