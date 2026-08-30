"""一覧の要求本文を貼れる雛形にする部分の検査。純粋なのでここで全部見られる。

**この道具が生まれた理由そのものを検査する。** 実測35回目、座標の雛形が偵察の
印 (``"<bool>"``) のままで HTTP 500 が返り、それが「0件」として現れかけた。
"""

from __future__ import annotations

import json

import pytest

from jobmedley_scout.api.payloads import assert_fully_filled
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.recon.search_payload import (
    WITHHELD_MARKER,
    as_template,
    is_empty,
)

#: 実測した本文の形を縮めたもの。**キーの名前は実物と同じ。**
CAPTURED = json.dumps(
    {
        "age": {"from": "0", "to": "40"},
        "customer_search_condition_id": 739599,
        "desired_features": [236, 255],
        "employment_types": [1],
        "favorite": False,
        "member_id": [],
        "nav_type": 25,
        "pagination": {"limit": 25, "page": 1},
        "sort": "recently_registered",
    }
)


def _parsed(body: str) -> dict[str, object]:
    result = as_template(body)
    assert result is not None
    return dict(json.loads(result.template))


def test_the_three_runtime_slots_carry_the_observed_kind() -> None:
    """**型は観測が決める。** 実装が決めれば、それは推測である (原則3)。

    この媒体は型が一貫していない (実測36回目)。同じ職種IDが
    ``desired_job_category_ids: ["10"]`` では文字列、
    ``career_job_categories[].job_category_id: 10`` では数値だった。
    """
    filled = _parsed(CAPTURED)
    # 観測では条件番号が数値、ページが数値だった。
    assert filled["customer_search_condition_id"] == "{{SEARCH_CONDITION_ID:number}}"
    assert filled["pagination"] == {"limit": "{{PAGE_SIZE:number}}", "page": "{{PAGE:number}}"}


def test_a_string_valued_slot_is_marked_as_a_string() -> None:
    """同じ欄でも、媒体が文字列で送っていれば文字列として運ぶ。"""
    body = json.dumps({**json.loads(CAPTURED), "pagination": {"limit": "25", "page": "1"}})
    result = as_template(body)
    assert result is not None
    assert json.loads(result.template)["pagination"] == {
        "limit": "{{PAGE_SIZE:string}}",
        "page": "{{PAGE:string}}",
    }


def test_a_slot_whose_kind_cannot_be_read_gets_no_marker() -> None:
    """**型が読めない欄に記法を置かない。** 置けば型をこちらで決めたことになる。

    記法が無ければ差し替えも起きず、``missing_slots`` に出て使えないと分かる。
    """
    body = json.dumps({**json.loads(CAPTURED), "pagination": {"limit": None, "page": 1}})
    result = as_template(body)
    assert result is not None
    assert json.loads(result.template)["pagination"]["limit"] is None
    assert result.missing_slots == ("pagination.limit",)
    assert result.usable() is False


def test_a_field_that_merely_shares_a_value_with_a_slot_is_left_alone() -> None:
    """**値の一致で探さない。** ``nav_type`` も 25 だが、これは差し替え対象ではない。

    値で探す実装だと ``pagination.limit`` を差し替えるついでに ``nav_type`` まで
    ``{{PAGE_SIZE}}`` にしてしまう。媒体は文字列を受け取り、絞り込みが静かに
    変わる -- 送信先が変わるほうの事故と違い、**返ってくる人が変わる**。
    """
    assert _parsed(CAPTURED)["nav_type"] == 25


def test_everything_else_is_kept_exactly_as_captured() -> None:
    """**運用者自身の検索条件なので、値ごと残す。** 伏せたら貼っても通らない。"""
    filled = _parsed(CAPTURED)
    assert filled["age"] == {"from": "0", "to": "40"}
    assert filled["desired_features"] == [236, 255]
    assert filled["favorite"] is False
    assert filled["sort"] == "recently_registered"


def test_an_empty_member_id_is_kept_because_it_names_nobody() -> None:
    """空を伏せると、運用者が「たぶん空だろう」と **推測で書く** ことになる (原則3)。"""
    result = as_template(CAPTURED)
    assert result is not None
    assert json.loads(result.template)["member_id"] == []
    assert result.withheld == ()


def test_a_filled_member_id_is_withheld_because_it_names_a_candidate() -> None:
    body = json.dumps({**json.loads(CAPTURED), "member_id": [3323741]})
    result = as_template(body)
    assert result is not None
    assert json.loads(result.template)["member_id"] == WITHHELD_MARKER
    assert result.withheld == ("member_id",)
    assert "3323741" not in result.template


def test_the_withheld_marker_stops_the_send_guard_rather_than_flying_as_is() -> None:
    """伏せた欄をそのまま貼ったら、**呼ぶ前に止まる**。伏せたことを人に決めさせる。"""
    body = json.dumps({**json.loads(CAPTURED), "member_id": [3323741]})
    result = as_template(body)
    assert result is not None
    with pytest.raises(ConfigError):
        assert_fully_filled(json.loads(result.template), used_by="test")


def test_the_produced_template_passes_the_guard_once_the_slots_are_filled() -> None:
    """**貼ってそのまま使える** ことを検査する。これが道具の目的である。

    残るのは3つの差し込み記法だけで、実行時に値が入る。``<...>`` は1つも残らない。
    """
    from jobmedley_scout.runtime.commands.ingest import (
        PAGE_SIZE_SLOT,
        PAGE_SLOT,
        SEARCH_CONDITION_SLOT,
        _substitute_slots,
    )

    result = as_template(CAPTURED)
    assert result is not None
    template = json.loads(result.template)
    with pytest.raises(ConfigError):
        # 差し込む前は「まだ値が決まっていない」ので止まる。
        assert_fully_filled(template, used_by="test")

    filled = _substitute_slots(
        template, {SEARCH_CONDITION_SLOT: "739599", PAGE_SLOT: 1, PAGE_SIZE_SLOT: 25}
    )
    assert_fully_filled(filled, used_by="test")  # 例外が出なければ通る
    # **観測どおりの型で入っている。** 観測は数値だった。
    assert filled["customer_search_condition_id"] == 739599
    assert filled["pagination"] == {"limit": 25, "page": 1}


def test_the_operators_own_condition_number_is_reported_for_cross_checking() -> None:
    """config.yaml の ingest.search_condition_id と突き合わせるために出す。"""
    result = as_template(CAPTURED)
    assert result is not None
    assert result.condition_id == "739599"


def test_a_body_missing_a_runtime_slot_is_reported_as_unusable() -> None:
    """**ページ番号が無い雛形は静かな重複を生む。** 使えないと言い切る (原則2)。"""
    body = json.dumps({"customer_search_condition_id": 1, "pagination": {"limit": 25}})
    result = as_template(body)
    assert result is not None
    assert result.usable() is False
    assert result.missing_slots == ("pagination.page",)


def test_an_unreadable_body_produces_nothing_rather_than_a_guess() -> None:
    """原則3。「たぶんこういう形」を作れば、それは推測で座標を埋めることになる。"""
    assert as_template("<html>not json</html>") is None
    assert as_template("[1, 2, 3]") is None
    assert as_template("") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        ("", True),
        ([], True),
        ({}, True),
        (0, False),
        (False, False),
        ("0", False),
        ([0], False),
    ],
)
def test_zero_and_false_are_not_empty(value: object, expected: bool) -> None:
    """``not value`` で書くと ``0`` を空と読む。**絞り込みの 0 は「指定なし」でありうる。**"""
    assert is_empty(value) is expected
