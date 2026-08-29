"""求人IDの取り出し。**送信payloadに残る `<...>` のうち2つがここで埋まる。**

このコマンドは他の偵察と違って **値を出す**。返るのは運用者自身が媒体へ公開
している求人票であり、13.2 が守ろうとしている対象 (候補者の氏名・会員番号・
年齢・居住地) ではない。伏せると、どのIDがどの求人かを運用者が突き合わせられず、
座標を埋める手段そのものが無くなる。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.recon.job_offers import (
    MAX_LABEL_CHARS,
    JobOffer,
    extract_job_offers,
    render_offers,
)
from jobmedley_scout.recon.observe_job_offers import OfferObservation, OfferStage

#: 実測したキーパス (``data.job_offers[].id`` / ``...job_offer_salaries[].id``) の形。
RESPONSE = {
    "data": {
        "job_offers": [
            {
                "id": 111,
                "name": "歯科衛生士",
                "employment_type": "正職員",
                "job_offer_salaries": [{"id": 222}],
            },
            {
                "id": 333,
                "name": "歯科助手",
                "employment_type": "パート",
                "job_offer_salaries": [{"id": 444}, {"id": 555}],
            },
        ]
    }
}


def test_ids_come_out_with_their_salary_ids() -> None:
    """**送信に要るのはこの2つ。** 片方だけでは payload が組み立たない。"""
    offers = extract_job_offers(RESPONSE)
    assert [o.offer_id for o in offers] == ["111", "333"]
    assert offers[0].salary_ids == ("222",)
    assert offers[1].salary_ids == ("444", "555")


def test_the_array_is_found_by_name_not_by_position() -> None:
    """封筒の形が変わっても壊れない。**位置を決め打ちしない。**"""
    assert extract_job_offers({"job_offers": RESPONSE["data"]["job_offers"]})
    assert extract_job_offers({"a": {"b": {"data": {"job_offers": [{"id": 7}]}}}})


def test_an_offer_without_a_readable_id_is_dropped() -> None:
    """IDが無ければ送信に使えない。**並びに残すと「選べない候補」になる。**"""
    assert extract_job_offers({"job_offers": [{"name": "IDなし"}, {"id": 9}]}) == (
        JobOffer(offer_id="9", salary_ids=(), labels=()),
    )
    # 真偽値をIDとして拾わない (bool は int の下位型)。
    assert extract_job_offers({"job_offers": [{"id": True}]}) == ()


def test_long_text_fields_are_not_printed() -> None:
    """**出す範囲を絞ってある。** 説明文まで印字すると報告が読めなくなる。"""
    long_text = "あ" * (MAX_LABEL_CHARS + 1)
    offers = extract_job_offers(
        {"job_offers": [{"id": 1, "name": "歯科衛生士", "body": long_text}]}
    )
    assert offers[0].labels == (("name", "歯科衛生士"),)


def test_nested_objects_are_not_walked_for_labels() -> None:
    """入れ子を辿ると、将来この応答が抱えるものを全部印字することになる。"""
    offers = extract_job_offers(
        {"job_offers": [{"id": 1, "name": "歯科衛生士", "owner": {"tel": "090-0000-0000"}}]}
    )
    assert offers[0].labels == (("name", "歯科衛生士"),)
    assert "090-0000-0000" not in render_offers(offers, wanted="歯科衛生士")


def test_a_missing_array_is_reported_as_such_not_as_zero_offers() -> None:
    """**「求人が無い」と「探し方が違う」を混ぜない** (原則2)。"""
    said = render_offers(extract_job_offers({"data": {}}), wanted="歯科衛生士")
    assert "1件も取り出せませんでした" in said
    assert "この報告からは決まりません" in said


def test_the_report_narrows_to_one_when_it_can() -> None:
    offers = extract_job_offers(RESPONSE)
    said = render_offers(offers, wanted="歯科衛生士")
    assert "1件に絞れました" in said
    assert "jobOfferId=111" in said
    assert "jobOfferSalaryId=222" in said


def test_multiple_salaries_are_not_picked_for_the_operator() -> None:
    """**推測しない** (原則3)。どの給与条件で送るかは運用者が決める。"""
    offers = extract_job_offers(
        {
            "job_offers": [
                {"id": 1, "name": "歯科衛生士 正職員", "job_offer_salaries": [{"id": 2}, {"id": 3}]}
            ]
        }
    )
    said = render_offers(offers, wanted="歯科衛生士")
    assert "給与条件が 2 つあります" in said
    assert "運用者が選んでください" in said


def test_no_match_says_so_rather_than_picking_the_first() -> None:
    said = render_offers(extract_job_offers(RESPONSE), wanted="保育士")
    assert "一致しませんでした" in said


def test_the_stage_chain_reports_the_stage_actually_reached() -> None:
    """**単調性。** 後の段の条件は、前の段の条件に含まれていなければならない。"""
    assert OfferObservation(requested_url="u", session_present=False).reached() is (
        OfferStage.NO_SESSION
    )
    assert OfferObservation(requested_url="u", session_expired=True).reached() is (
        OfferStage.SESSION_EXPIRED
    )
    assert OfferObservation(requested_url="u").reached() is OfferStage.NOT_ANSWERED
    assert OfferObservation(requested_url="u", answered=True).reached() is OfferStage.NO_OFFERS
    found = OfferObservation(requested_url="u", answered=True, offers=extract_job_offers(RESPONSE))
    assert found.reached() is OfferStage.FOUND


def test_a_state_that_contradicts_the_timeline_raises_instead_of_lying() -> None:
    """**報告を嘘にしないために止める。** 求人が在るのに応答が無いことはない。"""
    impossible = OfferObservation(
        requested_url="u", answered=False, offers=extract_job_offers(RESPONSE)
    )
    with pytest.raises(ValueError, match="時系列と矛盾"):
        impossible.reached()


def test_the_report_never_claims_a_send_happened() -> None:
    said = OfferObservation(
        requested_url="u",
        answered=True,
        offers=extract_job_offers(RESPONSE),
        listener_attached=True,
    ).render()
    assert "送信も1件もしていません" in said
    # **0件でも書く。** 黙ると「観測しなかった」と区別が付かない。
    assert "止めた通信 (他所のオリジンへ): 0 件" in said
