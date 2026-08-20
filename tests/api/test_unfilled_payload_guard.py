"""埋め忘れた payload を **送る前に** 止める。

実測20回目で観測した送信 payload には、こちらが値を用意しなければならない欄が
5つあった::

    jobOfferId  jobOfferSalaryId  memberId  scoutMessage  searchUuid

偵察は値を種別に伏せて記録する (``<number>`` / ``<string>``)。個人データを
ログへ残さないためである (13.2)。**その雛形をそのまま座標へ貼ると何が起きるか。**

差し込みは「知っている記法」しか置き換えない。``<string>`` は知らない記法なので
そのまま残り、``<string>`` という **文字列** がスカウトの本文として実在の候補者へ
飛ぶ。月次の送信枠を1通消費し、相手の受信箱に残り、取り消す手段は無い (13.6)。

これは「失敗する」形の事故ではない。**成功する形の事故** である。だから成功する
前に止める。「送れなかった」は次の実行でやり直せるが、「間違ったものを送った」は
やり直せない。
"""

from __future__ import annotations

import json

import pytest

from jobmedley_scout.api.payloads import (
    PLACEHOLDER_BODY,
    PLACEHOLDER_CANDIDATE_ID,
    assert_fully_filled,
    build_send_payload,
    unfilled_slots,
)
from jobmedley_scout.errors import ConfigError

#: 偵察が出したままの雛形 (実測20回目の形)。**貼っただけでは送れない。**
AS_RECONNED = json.dumps(
    {
        "operationName": "SendSingleScout",
        "query": "mutation SendSingleScout($input: SendSingleScoutInput!) { ok }",
        "variables": {
            "input": {
                "jobOfferId": "<number>",
                "jobOfferSalaryId": "<number>",
                "memberId": "<number>",
                "scoutMessage": PLACEHOLDER_BODY,
                "searchUuid": "<string>",
            }
        },
    },
    ensure_ascii=False,
)

#: 運用者が埋めたあとの雛形。
AS_FILLED = json.dumps(
    {
        "operationName": "SendSingleScout",
        "query": "mutation SendSingleScout($input: SendSingleScoutInput!) { ok }",
        "variables": {
            "input": {
                "jobOfferId": 14151,
                "jobOfferSalaryId": 22222,
                "memberId": PLACEHOLDER_CANDIDATE_ID,
                "scoutMessage": PLACEHOLDER_BODY,
                "searchUuid": "{{SEARCH_UUID}}",
            }
        },
    },
    ensure_ascii=False,
)


def test_the_reconned_template_is_refused_before_anything_is_sent() -> None:
    """**偵察の出力をそのまま貼ったら、送信は起動しない。**"""
    with pytest.raises(ConfigError) as caught:
        build_send_payload(
            AS_RECONNED,
            candidate_id="3323741",
            subject="",
            body="はじめまして。",
            followup_days=None,
            used_by="test",
        )
    said = str(caught.value)
    # 何が足りないのかを名指しする。「エラー」だけでは直せない。
    assert "variables.input.jobOfferId" in said
    assert "variables.input.searchUuid" in said


def test_the_body_is_the_one_slot_the_code_fills_itself() -> None:
    """本文だけは差し込まれる。**残りは人間か、上位が用意する。**"""
    payload = build_send_payload(
        AS_FILLED,
        candidate_id="3323741",
        subject="",
        body="はじめまして。ヤガサキ歯科医院の採用担当です。",
        followup_days=None,
        used_by="test",
        extra={"{{SEARCH_UUID}}": "b1e2-uuid"},
    )
    sent = payload["variables"]["input"]
    assert sent["scoutMessage"] == "はじめまして。ヤガサキ歯科医院の採用担当です。"
    assert sent["memberId"] == "3323741"
    assert sent["searchUuid"] == "b1e2-uuid"
    assert sent["jobOfferId"] == 14151


def test_an_unsupplied_extra_is_refused_rather_than_sent_as_its_own_name() -> None:
    """**知らない記法を素通しさせない。**

    ``{{SEARCH_UUID}}`` を渡し忘れたら、``"{{SEARCH_UUID}}"`` という文字列が
    そのまま媒体へ渡る。それは送信の失敗ではなく、**間違った送信の成功** である。
    """
    with pytest.raises(ConfigError) as caught:
        build_send_payload(
            AS_FILLED,
            candidate_id="3323741",
            subject="",
            body="本文",
            followup_days=None,
            used_by="test",
        )
    assert "variables.input.searchUuid" in str(caught.value)


def test_both_notations_count_as_unfilled() -> None:
    """種別の印も、差し込み記法も、「まだ値が決まっていない」という同じ事実である。"""
    assert unfilled_slots({"a": "<number>"}) == ("a",)
    assert unfilled_slots({"a": "{{BODY}}"}) == ("a",)
    assert unfilled_slots({"a": {"b": ["<string>"]}}) == ("a.b[0]",)
    assert unfilled_slots({"a": "ふつうの文字列"}) == ()
    # 山括弧を含むだけの文面は誤検知しない (本文に < > が出ることはありうる)。
    assert unfilled_slots({"a": "10<20 の件"}) == ()


def test_a_fully_filled_payload_passes_untouched() -> None:
    assert_fully_filled({"variables": {"input": {"memberId": 3323741}}}, used_by="test")


def test_the_query_document_is_not_mistaken_for_an_unfilled_slot() -> None:
    """GraphQL の問い合わせ文には ``$input: SendSingleScoutInput!`` のような記法が
    混ざるが、**あれは埋める欄ではない**。誤検知すると送信が永久に起動しない。
    """
    payload = json.loads(AS_FILLED)
    payload["variables"]["input"]["memberId"] = 3323741
    payload["variables"]["input"]["scoutMessage"] = "本文"
    payload["variables"]["input"]["searchUuid"] = "uuid"
    assert_fully_filled(payload, used_by="test")
