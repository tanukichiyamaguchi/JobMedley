"""座標に入っている **実物の** 送信payload雛形を、そのまま検査する。

13.4: 「送信APIは契約テストが唯一の防波堤です。」既存の契約テストは仮の雛形で
**形** を固定しているが、それは実際に配られている雛形が正しいことを言わない。

**実物を試験しないと、次のことが起きる。** 差し込みの記法を書き間違えても
(``{{CANDIDATE_ID}}`` を ``{{CANDIDATE}}`` と書く等) 試験は緑のまま、送信の
直前になって初めて落ちる。最悪なのは落ちない場合で、記法がそのまま候補者へ
飛ぶ -- 取り消せない (13.6)。

段階4-3 (1通だけの実送信) の直前で守るべきものが、ここに集まっている。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from jobmedley_scout.api.payloads import (
    PLACEHOLDER_BODY,
    PLACEHOLDER_CANDIDATE_ID,
    PLACEHOLDER_SEARCH_UUID,
    assert_fully_filled,
    assert_sendable_graphql,
    build_send_payload,
)
from jobmedley_scout.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
COORDINATES = REPO_ROOT / "config" / "site_coordinates.yaml"

#: 運用者が指定した求人 (ヤガサキ歯科医院 歯科衛生士 正職員)。
#: **2つの独立した経路が同じ値を指した** -- 求人一覧の名前突き合わせ (実測33回目) と、
#: 送信フォームの求人検索が選んだ値 (実測34回目)。
EXPECTED_JOB_OFFER_ID = 811220
EXPECTED_SALARY_ID = 3381377


def _template() -> str:
    loaded = yaml.safe_load(COORDINATES.read_text(encoding="utf-8"))
    raw = loaded["api.send.paid.payload_template"]
    assert isinstance(raw, str), "雛形が文字列ではありません"
    return raw


def test_the_shipped_template_is_valid_json_with_a_query_document() -> None:
    """**GraphQL は query の無いリクエストを受け付けない。**

    キーごと欠けている穴は、座標の UNRESOLVED 検査には映らない。
    """
    doc = json.loads(_template())
    assert doc["operationName"] == "SendSingleScout"
    assert_sendable_graphql(doc, used_by="test")
    # 応答の3本目の失敗経路がこの文書に在ることも固定する (api/success.py)。
    assert "errorMessage" in doc["query"]


def test_the_observed_job_ids_are_the_ones_the_operator_asked_for() -> None:
    """**運用者が言葉で指定した求人と、観測した値が結び付いていること。**

    ここが静かにずれると、**別の求人のスカウトが実在の候補者へ飛ぶ**。
    取り消せない (13.6)。
    """
    fields = json.loads(_template())["variables"]["input"]
    assert fields["jobOfferId"] == EXPECTED_JOB_OFFER_ID
    assert fields["jobOfferSalaryId"] == EXPECTED_SALARY_ID


def test_the_runtime_fields_use_the_vocabulary_the_code_substitutes() -> None:
    """**出力と入力の語彙を揃える。**

    記法を書き間違えても検査は緑のままで、送信の直前まで分からない。最悪なのは
    落ちない場合で、``{{CANDIDATE}}`` のような文字列がそのまま候補者へ飛ぶ。
    """
    fields = json.loads(_template())["variables"]["input"]
    assert fields["memberId"] == PLACEHOLDER_CANDIDATE_ID
    assert fields["scoutMessage"] == PLACEHOLDER_BODY
    assert fields["searchUuid"] == PLACEHOLDER_SEARCH_UUID


def test_the_template_alone_can_never_be_sent() -> None:
    """**差し込み前は必ず止まる。** 記法がスカウトとして飛ぶことは無い (13.6)。"""
    with pytest.raises(ConfigError) as excinfo:
        assert_fully_filled(json.loads(_template()), used_by="test")
    said = str(excinfo.value)
    for slot in ("memberId", "scoutMessage", "searchUuid"):
        assert slot in said
    # 観測で埋まった欄は宿題に数えない (数えると「未確定」の一覧が嘘になる)。
    assert "jobOfferId" not in said
    assert "jobOfferSalaryId" not in said


def test_the_shipped_template_fills_end_to_end() -> None:
    """**実行時の値が揃えば、実物がそのまま送れる形になること。**

    ここが通らなければ、段階4-3 は座標を直すところからやり直しになる。
    """
    filled = build_send_payload(
        _template(),
        candidate_id="1613058",
        subject="",
        body="本文です。",
        followup_days=None,
        used_by="test",
        extra={PLACEHOLDER_SEARCH_UUID: "aaaa-bbbb-cccc"},
    )
    fields = filled["variables"]["input"]
    assert fields == {
        "jobOfferId": EXPECTED_JOB_OFFER_ID,
        "jobOfferSalaryId": EXPECTED_SALARY_ID,
        "memberId": "1613058",
        "scoutMessage": "本文です。",
        "searchUuid": "aaaa-bbbb-cccc",
    }
    # 封筒は落ちない。query が無ければ媒体は受け付けない。
    assert "mutation SendSingleScout" in filled["query"]


def test_a_missing_search_uuid_stops_the_send_rather_than_sending_the_marker() -> None:
    """**searchUuid は build_send_payload の語彙に無く、``extra`` で渡す。**

    渡し忘れたときに黙って ``{{SEARCH_UUID}}`` が飛ぶと、送信は成立したうえで
    集計だけが壊れる。止める。
    """
    with pytest.raises(ConfigError, match="searchUuid"):
        build_send_payload(
            _template(),
            candidate_id="1613058",
            subject="",
            body="本文です。",
            followup_days=None,
            used_by="test",
        )
