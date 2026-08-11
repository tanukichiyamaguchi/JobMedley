"""10.3: 解析対象はDOMではなく一覧応答の本文。件名は推測しない。"""

from __future__ import annotations

import json

import pytest

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.text_norm import normalize_subject
from jobmedley_scout.reply.extract import RowSource, extract_rows, parse_body

SUBJECT_PATH = "data.items[].subject"

LISTING = json.dumps(
    {
        "data": {
            "items": [
                {
                    "id": 991,
                    "subject": "Re: 田中太郎様｜介護のお仕事のご紹介｜8/11",
                    "sender": "田中太郎",
                },
                {
                    "id": 992,
                    "subject": "Re: 山田花子様｜看護のお仕事のご紹介｜8/11",
                    "sender": "山田花子",
                },
            ],
            "total": 2,
        }
    },
    ensure_ascii=False,
)


def test_rows_come_from_the_response_body_at_the_coordinate() -> None:
    rows = extract_rows(LISTING, SUBJECT_PATH)

    assert len(rows) == 2
    assert [row.row_index for row in rows] == [0, 1]
    assert all(row.source is RowSource.JSON_PATH for row in rows)
    assert rows[0].subject == "Re: 田中太郎様｜介護のお仕事のご紹介｜8/11"
    # 8.6: 突合キーの正規化は抽出時に済ませる。呼び出し側が忘れる経路を作らない。
    assert rows[0].subject_norm == normalize_subject(rows[0].subject or "")
    assert "Re:" not in (rows[0].subject_norm or "")


def test_rows_carry_the_other_recoverable_fields_for_diagnosis() -> None:
    rows = extract_rows(LISTING, SUBJECT_PATH)

    assert dict(rows[0].fields)["id"] == "991"
    assert dict(rows[0].fields)["sender"] == "田中太郎"


def test_without_the_coordinate_rows_are_recovered_but_the_subject_is_never_invented() -> None:
    """件名の座標は段階3で確定する。確定するまでは行の存在だけを返す。"""
    rows = extract_rows(LISTING, None)

    assert len(rows) == 2
    assert all(row.source is RowSource.STRUCTURAL_SCAN for row in rows)
    assert [row.subject for row in rows] == [None, None]
    assert [row.subject_norm for row in rows] == [None, None]
    # 件名らしき値は fields に残るので、座標の特定はここから行える (10.6)。
    assert "subject" in dict(rows[0].fields)


def test_a_coordinate_that_no_longer_applies_falls_back_instead_of_returning_zero() -> None:
    """静かに0件を返すと「返信ゼロ」に見える (原則2)。行の存在だけは返す。"""
    rows = extract_rows(LISTING, "payload.messages[].title")

    assert len(rows) == 2
    assert all(row.source is RowSource.STRUCTURAL_SCAN for row in rows)
    assert all(row.subject is None for row in rows)


def test_an_empty_page_at_the_coordinate_stays_empty() -> None:
    """最終ページの0件を「座標が外れた」と誤解して構造走査に落とすと終端判定が壊れる。"""
    body = json.dumps({"data": {"items": [], "total": 0, "filters": [{"name": "unread"}]}})

    assert extract_rows(body, SUBJECT_PATH) == ()


def test_a_non_string_subject_is_not_turned_into_one() -> None:
    body = json.dumps({"data": {"items": [{"subject": None, "id": 1}, {"subject": 42, "id": 2}]}})

    rows = extract_rows(body, SUBJECT_PATH)

    assert len(rows) == 2
    assert [row.subject for row in rows] == [None, None]


def test_a_non_json_body_yields_no_rows() -> None:
    """応答がJSONとは限らない。行に仕立てず、診断は diagnostics 側で採る。"""
    assert extract_rows("<html><body>maintenance</body></html>", SUBJECT_PATH) == ()
    assert parse_body("<html>") is None


def test_the_structural_scan_prefers_the_largest_list_of_objects() -> None:
    body = json.dumps(
        {
            "menu": [{"label": "home"}],
            "result": {"rows": [{"id": 1}, {"id": 2}, {"id": 3}]},
        }
    )

    rows = extract_rows(body, None)

    assert len(rows) == 3
    assert dict(rows[2].fields)["id"] == "3"


def test_nested_objects_are_flattened_with_dotted_keys() -> None:
    body = json.dumps({"items": [{"id": 1, "from": {"name": "田中太郎", "kind": "candidate"}}]})

    rows = extract_rows(body, None)

    assert dict(rows[0].fields)["from.name"] == "田中太郎"


def test_a_path_without_a_repeat_marker_yields_a_single_row() -> None:
    body = json.dumps({"detail": {"subject": "田中太郎様｜介護のお仕事のご紹介｜8/11"}})

    rows = extract_rows(body, "detail.subject")

    assert len(rows) == 1
    assert rows[0].subject_norm == normalize_subject("田中太郎様｜介護のお仕事のご紹介｜8/11")


def test_an_indexed_path_segment_is_supported() -> None:
    body = json.dumps({"pages": [{"items": [{"subject": "件名です"}]}]})

    rows = extract_rows(body, "pages[0].items[].subject")

    assert [row.subject for row in rows] == ["件名です"]


def test_an_unparseable_path_is_refused_loudly() -> None:
    with pytest.raises(ConfigError):
        extract_rows(LISTING, "data.items[0[].subject")


def test_two_repeat_markers_are_refused() -> None:
    """行の繰り返しが2段あると行数の意味が定まらない。推測せず落とす。"""
    with pytest.raises(ConfigError, match=r"\[\]"):
        extract_rows(LISTING, "data[].items[].subject")
