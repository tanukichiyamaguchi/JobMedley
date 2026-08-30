"""**一覧の行が持っている材料を捨てない。** 実測40回目で分かった穴。

レジュメが読めず、モデルへ渡った人物の事実は **会員番号と市名の2つだけ** だった。
材料ゼロで「共感を書け」と言われれば創作しか残らず、実際に創作された。

ところが一覧の行には材料が載っていた。座標ファイルには、こう書いてある。

    # 文面の設計がこれに従う。…代わりに使える手掛かりは一覧の応答に載っている:
    #     qualifications[].name            資格名
    #     member_career_job_categories[]   経験職種と年数
    #     member_desired_job_categories[]  希望職種
    #     short_address / desired_cities   居住地・希望勤務地

**設計として書かれていて、実装が読んでいなかった。**

値の形は観測していないので、文字列の配列とオブジェクトの配列の両方を受ける。
そして「どちらだったか」を報告する -- 外したら黙って「非公開」になるからである。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.api.candidates import (
    candidate_from_row,
    describe_row_shapes,
    list_facts_from_row,
)
from jobmedley_scout.generation.facts import UNDISCLOSED
from jobmedley_scout.generation.scout_message import candidate_slots
from jobmedley_scout.models.candidate import Candidate, ResumeFacts

#: 検査用の行。**実在の候補者ではない。**
ROW: dict[str, object] = {
    "id": 1000,
    "code": "00000000",
    "short_address": "神奈川県鎌倉市",
    "age": 27,
    "qualifications": [{"name": "歯科衛生士"}],
    "desired_cities": ["神奈川県鎌倉市", "神奈川県藤沢市"],
    "member_desired_job_categories": [{"name": "歯科衛生士"}],
    "member_career_job_categories": [{"name": "歯科衛生士", "career_year": 4}],
}


def _slots(row: dict[str, object]) -> dict[str, str]:
    candidate = candidate_from_row(row)
    assert candidate is not None
    return candidate_slots(candidate)


def test_the_list_row_alone_already_gives_the_model_real_material() -> None:
    """**これが穴だった。** レジュメが読めなくても、ここまでは渡せる。"""
    slots = _slots(ROW)
    assert slots["QUALIFICATIONS"] == "歯科衛生士"
    assert slots["CAREER_YEARS"] == "歯科衛生士: 4年"
    assert "神奈川県鎌倉市" in slots["DESIRED_CONDITIONS"]
    assert slots["RESIDENCE"] == "神奈川県鎌倉市"


def test_a_row_with_no_material_still_says_undisclosed_rather_than_inventing() -> None:
    """**空欄は「非公開」として渡る。** 省略するとモデルは渡し忘れと読む (8.3)。"""
    slots = _slots({"id": 1000, "code": "00000000"})
    assert slots["QUALIFICATIONS"] == UNDISCLOSED
    assert slots["CAREER_YEARS"] == UNDISCLOSED


@pytest.mark.parametrize(
    "qualifications",
    [
        [{"name": "歯科衛生士"}],
        ["歯科衛生士"],
        [{"label": "歯科衛生士"}],
    ],
)
def test_either_array_shape_is_read(qualifications: list[object]) -> None:
    """**形を観測していないので決め打ちしない** (原則3)。

    文字列の配列でもオブジェクトの配列でも読む。外したら黙って0件になる。
    """
    facts = list_facts_from_row({**ROW, "qualifications": qualifications})
    assert facts.qualifications == ("歯科衛生士",)


def test_an_occupation_without_a_year_does_not_reach_the_years_slot() -> None:
    """**年数が無ければ職種名も渡さない。**

    年数の欄に職種名だけを入れると、モデルは欄を埋めるために年数を自分で作る。
    レジュメ側で一度直したのと同じ穴である。
    """
    facts = list_facts_from_row({**ROW, "member_career_job_categories": [{"name": "歯科衛生士"}]})
    assert facts.experienced_occupation_years == ()
    assert facts.experienced_occupations == ("歯科衛生士",)


# ---------------------------------------------------------------------------
# 出所を混ぜない
# ---------------------------------------------------------------------------


def test_the_resume_wins_where_it_has_a_value() -> None:
    """レジュメは厚い。**両方あればレジュメを採る。**"""
    candidate = Candidate(
        candidate_id="1",
        raw_id_observed="1",
        list_facts=ResumeFacts(qualifications=("一覧の資格",), age=27),
        resume=ResumeFacts(qualifications=("レジュメの資格",)),
    )
    merged = candidate.facts()
    assert merged.qualifications == ("レジュメの資格",)
    # **レジュメに無い欄は一覧が埋める。** 差し替えではなく重ね合わせである。
    assert merged.age == 27


def test_the_two_sources_stay_separate_on_the_model() -> None:
    """**混ぜて持たない。** 混ぜると、レジュメが読めているかが後から分からない。"""
    candidate = Candidate(
        candidate_id="1",
        raw_id_observed="1",
        list_facts=ResumeFacts(qualifications=("一覧の資格",)),
    )
    assert candidate.resume.qualifications == ()
    assert candidate.list_facts.qualifications == ("一覧の資格",)


# ---------------------------------------------------------------------------
# 外したことが分かる仕掛け
# ---------------------------------------------------------------------------


def test_the_shape_of_each_material_key_is_reported() -> None:
    """**形を観測していない欄を読みに行っている。** 外したら分かる必要がある。"""
    notes = "\n".join(describe_row_shapes(ROW))
    assert "qualifications: 1 件中 1 件読めました" in notes
    assert "age: 読めました" in notes


def test_a_key_the_platform_stopped_sending_is_named() -> None:
    """**「キーがありません」と「空の配列」を分ける。**

    前者は媒体が変わったか経路が違う。後者はこの候補者に記載が無いだけである。
    打つ手が違うので、同じ言葉にしてはいけない (原則2)。
    """
    row = {k: v for k, v in ROW.items() if k != "qualifications"}
    assert "qualifications: キーがありません" in "\n".join(describe_row_shapes(row))
    empty = "\n".join(describe_row_shapes({**ROW, "qualifications": []}))
    assert "qualifications: 空の配列 (この候補者に記載が無い)" in empty


def test_a_shape_we_guessed_wrong_shows_up_as_unread() -> None:
    """**読めた件数を出す。** 「4件中0件」なら形を外したと即座に分かる。"""
    row = {**ROW, "qualifications": [{"unexpected_key": "歯科衛生士"}]}
    assert "qualifications: 1 件中 0 件読めました" in "\n".join(describe_row_shapes(row))


def test_the_shape_report_never_carries_a_value() -> None:
    """13.2。**キー名と件数と種別だけ。**"""
    row = {**ROW, "qualifications": [{"name": "秘密の資格名"}]}
    assert "秘密の資格名" not in "\n".join(describe_row_shapes(row))


def test_an_undisclosed_age_is_not_reported_as_our_mistake() -> None:
    """**``null`` は「読めない形」ではない** (実測41回目)。

    実測41回目の報告は ``age: 読めない形です`` だった。だが ``null`` なら、
    それは **この候補者が年齢を公開していない** という観測である。同じ言葉に
    すると、こちらの形の外しと候補者の未記入が区別できない -- 直せないものを
    直そうとすることになる (原則2)。
    """
    notes = "\n".join(describe_row_shapes({**ROW, "age": None}))
    assert "age: null (この候補者が公開していません)" in notes


def test_a_shape_we_really_got_wrong_still_says_so_with_the_type() -> None:
    """**こちらの間違いは、こちらの間違いとして出す。** 型の名前だけ出す。"""
    notes = "\n".join(describe_row_shapes({**ROW, "age": {"value": 27}}))
    assert "age: 読めない形です (dict)" in notes
    assert "27" not in notes


def test_an_empty_string_age_is_the_candidate_s_choice_not_our_mistake() -> None:
    """**文字列でも中身で意味が違う** (実測45回目)。

    空なら未記入であり、こちらの形の外しではない。同じ言葉にすると、直せない
    ものを直そうとすることになる。
    """
    notes = "\n".join(describe_row_shapes({**ROW, "age": "   "}))
    assert "age: 空の文字列 (この候補者が公開していません)" in notes


def test_a_non_numeric_age_string_says_the_spelling_differs() -> None:
    """「20代」のような綴りなら、こちらが読み方を決め直す番である。**値は出さない。**"""
    notes = "\n".join(describe_row_shapes({**ROW, "age": "20代"}))
    assert "age: 数字ではない文字列です (綴りが想定と違います)" in notes
    assert "20代" not in notes


def test_a_missing_age_key_is_its_own_answer() -> None:
    """キーごと無いのは媒体が変わった合図であり、未記入とは違う。"""
    row = {k: v for k, v in ROW.items() if k != "age"}
    assert "age: キーがありません" in "\n".join(describe_row_shapes(row))
