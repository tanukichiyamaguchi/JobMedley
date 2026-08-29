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
    MAX_LABELS,
    JobOffer,
    envelope_meta,
    extract_job_offers,
    render_offers,
    widen_limit,
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


# ---------------------------------------------------------------------------
# 実測32回目で分かったこと。**報告が読めず、欲しい求人も入っていなかった。**
# ---------------------------------------------------------------------------

#: 実測32回目にそのまま出た形。80字に収まるが **改行を含む** 欄が多い。
REAL_SHAPE = {
    "data": {
        "job_offers": [
            {
                "id": 1711534,
                "type": "JMJobOffer",
                "appeal_title": "【歯科医院のコールセンター】",
                "holiday": "木・日・祝　※祝日週は木曜振替診療あり",
                "welfare_programs": "【社会保険】\n・健康保険、厚生年金保険\n・車通勤：車通勤不可",
                "training": "研修期間3か月\nマニュアルを用いて指導",
                "job_title": "コールセンター",
                "job_category_name": "医療事務/受付（コールセンター）",
                "suggest_name": (
                    "神奈川県 医療法人幸明会 ヤガサキ歯科医院 医療事務/受付 (コールセンター)"
                ),
                "job_offer_salaries": [{"id": 6982360}, {"id": 6982361}],
            }
        ],
        "total": 1,
        "limit": 1,
        "page": 1,
    }
}


def test_multiline_fields_are_not_used_as_labels() -> None:
    """**80字以内でも改行入りなら見出しにしない。**

    実測32回目、求人票の備考欄 (福利厚生・研修・休日) が80字に収まって通り、
    報告が改行で散らばって読めなくなった。長さだけでは本文と見出しを分けられない。
    """
    offer = extract_job_offers(REAL_SHAPE)[0]
    keys = [k for k, _ in offer.labels]
    assert "welfare_programs" not in keys
    assert "training" not in keys
    # 見出しの **値** に改行が無いこと (render() 自体は複数行の塊である)。
    assert all("\n" not in value for _, value in offer.labels)


def test_identifying_fields_come_first_and_the_count_is_capped() -> None:
    """全部出すと読めない。**身元を指す欄を名前で優先する。**"""
    offer = extract_job_offers(REAL_SHAPE)[0]
    assert len(offer.labels) <= MAX_LABELS
    assert offer.labels[0][0] == "suggest_name"


def test_the_envelope_gauges_are_reported() -> None:
    """**「1件しか無い」と「1件しか返っていない」を分ける。**

    実測32回目、返った求人は1件で、画面に在るはずの歯科衛生士の求人が
    入っていなかった。求人の並びだけを見ても、どちらなのかは決まらない。
    """
    meta = envelope_meta(REAL_SHAPE)
    assert ("total", "1") in meta
    assert ("limit", "1") in meta
    # 求人の並びそのものは目盛りに混ぜない。
    assert not any(k == "job_offers" for k, _ in meta)
    said = render_offers(extract_job_offers(REAL_SHAPE), wanted="歯科衛生士", meta=meta)
    assert "封筒の目盛り" in said
    assert "total = 1" in said


def test_the_gauges_are_found_next_to_the_array_not_at_the_root() -> None:
    """目盛りは並びの隣に置かれる。**持ち主を探して、そこから読む。**"""
    assert envelope_meta({"a": {"b": {"job_offers": [], "total": 7}}}) == (("total", "7"),)
    assert envelope_meta({"total": 7}) == ()  # 並びが無ければ目盛りも無い


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://h/api/x/?limit=1", "limit=100"),
        ("https://h/api/x/?page=1&limit=1", "limit=100"),
        ("https://h/api/x/", "limit=100"),
        ("https://h/api/x/?q=%E6%AD%AF%E7%A7%91", "limit=100"),
    ],
)
def test_only_the_limit_is_rewritten(url: str, expected: str) -> None:
    """**経路もパラメータ名もこちらで組み立てない** (原則3)。

    観測したURLをそのまま使い、``limit`` の値だけ差し替える。組み立てた瞬間、
    当たったのかどうかが分からなくなる。
    """
    out = widen_limit(url)
    assert expected in out
    assert out.startswith("https://h/api/x/")
    assert out.count("limit=") == 1


def test_other_query_parameters_survive_the_rewrite() -> None:
    """引き直しで検索条件を落とすと、**違う質問への答えを見ることになる。**"""
    out = widen_limit("https://h/api/x/?page=3&q=%E6%AD%AF%E7%A7%91&limit=1")
    assert "page=3" in out
    assert "q=%E6%AD%AF%E7%A7%91" in out


def test_the_report_says_when_the_refetch_itself_failed() -> None:
    """**黙ると「引き直したが増えなかった」と読める。** それは嘘になる。"""
    said = OfferObservation(
        requested_url="u",
        answered=True,
        offers=extract_job_offers(REAL_SHAPE),
        listener_attached=True,
        widened="引き直せませんでした (TimeoutError)",
    ).render()
    assert "引き直せませんでした" in said
