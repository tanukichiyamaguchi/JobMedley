"""取り込みの写像が、**観測できている事実を捨てていないこと**。

2026-08-30 の敵対的レビューが2件見つけた。どちらも「捨てた分をモデルが埋める」
という同じ形の事故で、埋めた内容はそのまま候補者へ届く (原則3 / 13.6)。
"""

from __future__ import annotations

from jobmedley_scout.api.candidates import candidate_from_row, resume_from_response

# ---------------------------------------------------------------------------
# 2026-08-30 のレビューが見つけた2件。**どちらも推測を招く形だった** (原則3)。
# ---------------------------------------------------------------------------


def test_the_observed_career_years_are_not_thrown_away() -> None:
    """**label だけを写すと、モデルが年数を作る。**

    媒体は ``careerJobCategories[] = {jobCategoryId, label, careerYear}`` を返す。
    careerYear が年数である (実画面の「歯科衛生士(3年)」の3年)。プロンプトは
    「経験年数」という欄を持ち、STEP2 がそこから語らせるので、必ず数字が要る。
    観測できている事実を捨てて推測させてはいけない。
    """
    response = {
        "data": {
            "memberGet": {
                "member": {
                    "appeal": {
                        "careerJobCategories": [
                            {"jobCategoryId": 1, "label": "歯科衛生士", "careerYear": 3},
                            {"jobCategoryId": 2, "label": "歯科助手", "careerYear": 1},
                        ]
                    }
                }
            }
        }
    }
    facts = resume_from_response(
        response,
        keypaths={"experienced_occupations": "data.memberGet.member.appeal.careerJobCategories"},
    )
    assert facts.experienced_occupation_years == ("歯科衛生士(3年)", "歯科助手(1年)")
    # 職種名だけの欄はそのまま残る (別の軸である)。
    assert facts.experienced_occupations == ("歯科衛生士", "歯科助手")


def test_an_entry_without_a_readable_year_is_dropped_from_the_years_field() -> None:
    """**年数なしで並べない。** 職種名が経験年数の欄に現れて年数のように読まれる。"""
    response = {
        "data": {
            "memberGet": {
                "member": {
                    "appeal": {
                        "careerJobCategories": [
                            {"label": "歯科衛生士", "careerYear": None},
                            {"label": "歯科助手", "careerYear": True},
                            {"label": "保育士", "careerYear": 2},
                        ]
                    }
                }
            }
        }
    }
    facts = resume_from_response(
        response,
        keypaths={"experienced_occupations": "data.memberGet.member.appeal.careerJobCategories"},
    )
    assert facts.experienced_occupation_years == ("保育士(2年)",)


def test_a_withheld_residence_becomes_absent_not_a_literal_address() -> None:
    """**伏せ字を通す。**

    媒体は非公開の欄に「（未応募のため非表示）」を入れて返すことがある。
    素通しすると、その文字列が居住地としてプロンプトへ渡り、モデルは
    それを地名として扱う -- 通勤時間をそこから作る。
    """
    withheld = candidate_from_row(
        {"id": 1, "code": "01613058", "short_address": "（未応募のため非表示）"}
    )
    assert withheld is not None
    assert withheld.residence is None

    real = candidate_from_row(
        {"id": 1, "code": "01613058", "short_address": "神奈川県川崎市宮前区"}
    )
    assert real is not None
    assert real.residence == "神奈川県川崎市宮前区"
