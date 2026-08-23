"""実測した応答を、値の意味を取り違えずにモデルへ写す。

6.4 の事故はこのファイルが守る境界そのものである。

> 参照実装の最大の罠は、レジュメのトップレベルの「業界」「職種」が *経験してきた*
> もので、希望条件オブジェクト配下の同名キーが *希望する* ものだったこと。

この媒体では ``appeal`` (経験) と ``desiredCondition`` (希望) で包みが分かれる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobmedley_scout.api.candidates import (
    candidate_from_row,
    resume_from_response,
    resume_keypaths,
    rows_in,
    search_uuid_in,
    unresolved_resume_fields,
    value_at,
)
from jobmedley_scout.config.loader import load_site_coordinates

COORDINATES = load_site_coordinates(Path("config/site_coordinates.yaml"))
KEYPATHS = resume_keypaths(COORDINATES)

#: 実測した応答の形 (キー名は本物、値は作り物)。
RESUME = {
    "data": {
        "memberGet": {
            "member": {
                "id": 3323741,
                "careers": [],
                "appeal": {
                    "isUnqualified": False,
                    "selfPr": "訪問診療に関心があります。",
                    "qualifications": [
                        {"id": 1, "name": "歯科衛生士", "isScheduled": False},
                        {"id": 2, "name": "認定歯科衛生士", "isScheduled": True},
                    ],
                    "careerJobCategories": [
                        {"jobCategoryId": 10, "label": "歯科衛生士", "careerYear": 3}
                    ],
                },
                "personalInformation": {
                    "address": {
                        "prefecture": {"name": "神奈川県"},
                        "city": {"name": "川崎市多摩区"},
                    },
                    "age": 24,
                    "gender": "女性",
                    "employmentStatus": "就業中",
                },
                "latestEducationBackground": {
                    "major": "歯科衛生士科",
                    "schoolType": "専門学校",
                    "department": "衛生課程",
                },
                "desiredCondition": {
                    "jobCategories": [
                        {"yearsOfExperience": 3, "jobCategory": {"id": 10, "name": "歯科衛生士"}}
                    ],
                    "workplaces": [
                        {"prefecture": {"name": "東京都"}, "city": {"name": "立川市"}},
                        {"prefecture": {"name": "東京都"}, "city": None},
                    ],
                    "features": [
                        {"id": 1, "name": "社会保険完備"},
                        {"id": 2, "name": "ネイルOK"},
                    ],
                },
            }
        }
    }
}


def _facts(**overrides: object) -> object:
    return resume_from_response(RESUME, keypaths=KEYPATHS)


# ---------------------------------------------------------------------------
# 一覧
# ---------------------------------------------------------------------------


def test_rows_and_the_search_identifier_come_from_the_same_response() -> None:
    """**1回の取得で候補者と検索識別子が揃う。** 別経路は要らない。"""
    page = {"members": [{"id": 1}, {"id": 2}], "search_uuid": "abc", "total": 2}
    assert len(rows_in(page)) == 2
    assert search_uuid_in(page) == "abc"


def test_a_missing_search_identifier_is_none_rather_than_empty_string() -> None:
    """スキーマ上 ``searchUuid`` は省略できる。**無いことを空文字にしない。**"""
    assert search_uuid_in({"members": []}) is None
    assert search_uuid_in({"members": [], "search_uuid": ""}) is None


def test_a_row_becomes_a_candidate_without_a_name() -> None:
    """**``code`` を氏名の欄へ入れない。** 会員番号であって名前ではない。"""
    candidate = candidate_from_row({"id": 3323741, "code": "01613058", "age": "20代"})
    assert candidate is not None
    assert candidate.candidate_id == "3323741"
    assert candidate.display_name is None


def test_a_row_without_an_id_is_dropped_rather_than_invented() -> None:
    assert candidate_from_row({"code": "01613058"}) is None
    assert candidate_from_row({"id": "  "}) is None


# ---------------------------------------------------------------------------
# 6.4: 経験と希望
# ---------------------------------------------------------------------------


def test_experienced_and_desired_occupations_do_not_swap() -> None:
    """**同じ「歯科衛生士」でも、経験と希望は別の欄から来る。**

    この試験は両方が同じ値でも通る -- 守っているのは値ではなく **経路** である。
    経路が入れ替われば、``appeal`` を消したときに希望側まで消える。
    """
    facts = resume_from_response(RESUME, keypaths=KEYPATHS)
    assert facts.experienced_occupations == ("歯科衛生士",)
    assert facts.desired_occupations == ("歯科衛生士",)

    without_experience = {
        "data": {
            "memberGet": {
                "member": {
                    **RESUME["data"]["memberGet"]["member"],  # type: ignore[dict-item,index]
                    "appeal": {"qualifications": [], "careerJobCategories": [], "selfPr": None},
                }
            }
        }
    }
    narrowed = resume_from_response(without_experience, keypaths=KEYPATHS)
    assert narrowed.experienced_occupations == ()
    # **希望側は残る。** 経路が分かれている証拠である。
    assert narrowed.desired_occupations == ("歯科衛生士",)


def test_industries_stay_empty_because_this_platform_has_none() -> None:
    """業界の軸が無い媒体で、職種を業界の欄へ入れない (6.4 そのもの)。"""
    facts = resume_from_response(RESUME, keypaths=KEYPATHS)
    assert facts.experienced_industries == ()
    assert facts.desired_industries == ()


# ---------------------------------------------------------------------------
# 資格: 保有と取得予定を混ぜない
# ---------------------------------------------------------------------------


def test_acquired_and_scheduled_qualifications_are_kept_apart() -> None:
    """**「◯◯をお持ちの方へ」を、まだ持っていない人に送らない。**"""
    facts = resume_from_response(RESUME, keypaths=KEYPATHS)
    assert facts.qualifications == ("歯科衛生士",)
    assert facts.qualifications_scheduled == ("認定歯科衛生士",)


def test_an_unreadable_scheduled_flag_never_counts_as_acquired() -> None:
    """**迷ったら送らない側へ倒す** (13.6)。"""
    payload = {
        "data": {
            "memberGet": {
                "member": {
                    "appeal": {
                        "qualifications": [{"name": "歯科衛生士", "isScheduled": "yes"}],
                        "careerJobCategories": [],
                    }
                }
            }
        }
    }
    facts = resume_from_response(payload, keypaths=KEYPATHS)
    assert facts.qualifications == ()


# ---------------------------------------------------------------------------
# 個別化の材料
# ---------------------------------------------------------------------------


def test_the_features_that_replace_the_missing_name_are_carried() -> None:
    """氏名が無い媒体では、こだわり条件が個別化の主材料になる。"""
    facts = resume_from_response(RESUME, keypaths=KEYPATHS)
    assert facts.desired_features == ("社会保険完備", "ネイルOK")


def test_places_are_joined_and_a_prefecture_only_wish_survives() -> None:
    facts = resume_from_response(RESUME, keypaths=KEYPATHS)
    assert facts.desired_locations == ("東京都立川市", "東京都")


def test_self_pr_is_carried_but_a_withheld_one_is_not() -> None:
    facts = resume_from_response(RESUME, keypaths=KEYPATHS)
    assert facts.self_pr == "訪問診療に関心があります。"

    blank = {"data": {"memberGet": {"member": {"appeal": {"selfPr": "未入力"}}}}}
    assert resume_from_response(blank, keypaths=KEYPATHS).self_pr is None


# ---------------------------------------------------------------------------
# 写していないもの
# ---------------------------------------------------------------------------


def test_employments_stay_empty_until_the_values_are_seen() -> None:
    """**キーパスは分かっているが、要素の意味を観測していない。**

    ``position`` が役職なのか職種なのかを1件も見ていない。空なら「非公開」と
    して渡り、モデルは職歴に言及できない (6.4 の手順3)。
    """
    payload = {
        "data": {
            "memberGet": {
                "member": {
                    "careers": [{"id": 1, "position": "主任", "jobContent": "訪問診療"}],
                    "appeal": {"qualifications": [], "careerJobCategories": []},
                }
            }
        }
    }
    assert resume_from_response(payload, keypaths=KEYPATHS).employments == ()


def test_the_school_name_is_not_invented_for_an_unapplied_member() -> None:
    """``schoolName`` は AppliedMember にしか無い。**返ってこないものを埋めない。**"""
    facts = resume_from_response(RESUME, keypaths=KEYPATHS)
    (education,) = facts.educations
    assert education.school is None
    assert education.raw_level == "専門学校"
    assert education.faculty == "歯科衛生士科"


def test_a_boolean_age_is_not_read_as_a_number() -> None:
    """``bool`` は ``int`` の部分型である。素通しすると True が1歳になる。"""
    payload = {"data": {"memberGet": {"member": {"personalInformation": {"age": True}}}}}
    assert resume_from_response(payload, keypaths=KEYPATHS).age is None


# ---------------------------------------------------------------------------
# 座標と経路
# ---------------------------------------------------------------------------


def test_every_required_resume_axis_is_resolved() -> None:
    """**未確定のまま取り込まない** (原則2)。

    未確定のまま通せば、その項目は永久に「非公開」で渡り続け、誰も気付かない。
    """
    assert unresolved_resume_fields(COORDINATES) == ()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("data.memberGet.member.personalInformation.age", 24),
        ("data.memberGet.member.appeal.selfPr", "訪問診療に関心があります。"),
        ("data.memberGet.member.missing", None),
        ("data.memberGet.member.appeal.qualifications.name", None),
        ("", None),
    ],
)
def test_value_at_follows_dictionaries_only(path: str, expected: object) -> None:
    """**配列は辿らない。** 辿ると「1件目だけ見た」のか分からなくなる。"""
    assert value_at(RESUME, path) == expected
