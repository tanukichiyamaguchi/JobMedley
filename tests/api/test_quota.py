"""送信枠の残数を読む。**座標は満たしていたが、コードは読んでいなかった。**

座標ファイルの注記にはこう書いてある (2026-08-21 に観測)。

    **これは 8章の「残数は毎回読む」を満たす**。

満たしていたのは **座標だけ** だった。2026-09-12 に本番の送信が ``HTTP 200`` かつ
``data.result.errorMessage`` 有りで失敗し、原因を追う途中で
``api.quota.url_pattern`` の呼び出し元が **0件** であることが分かった。

媒体のエラー文言は 13.2 のため記録していないので、枠を使い切っていても
**理由の分からない失敗** として現れる。読んでいれば報告に出ていた。
"""

from __future__ import annotations

import pytest

from jobmedley_scout.api.quota import (
    QuotaReading,
    reading_from_response,
    should_stop,
)

# --------------------------------------------------------------------------
# 読む
# --------------------------------------------------------------------------


def test_the_observed_shape_is_read() -> None:
    """2026-08-21 に観測した形。"""
    reading = reading_from_response({"remaining_count": 51, "total_count": 62})
    assert reading.remaining == 51
    assert reading.total == 62
    assert reading.readable()


def test_a_count_that_arrives_as_a_string_is_still_read() -> None:
    """**寛容に読む** (7.7)。JSON の数は文字列で来ることがある。"""
    reading = reading_from_response({"remaining_count": "7", "total_count": "62"})
    assert reading.remaining == 7


def test_the_total_may_be_missing_without_losing_the_remainder() -> None:
    """残数が読めれば、総数が無くても使える。"""
    reading = reading_from_response({"remaining_count": 3})
    assert reading.remaining == 3
    assert reading.total is None
    assert reading.readable()


# --------------------------------------------------------------------------
# 読めない -- **0 に畳まない**
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"remaining": 5},
        {"remaining_count": "たくさん"},
        {"remaining_count": None},
        {"remaining_count": True},
    ],
)
def test_an_unreadable_response_is_not_zero(body: dict[str, object] | None) -> None:
    """**ここが要点である。** 読めないことを「枠切れ」に畳まない。

    畳むと、応答の形が変わった日に送信が全部止まる -- 「静かなゼロ件」の別の顔で
    ある。逆に「残っている」に畳めば、枠が無いのに送り始める。どちらも事故なので
    三値のまま持つ (原則3)。
    """
    reading = reading_from_response(body)
    assert reading.remaining is None
    assert reading.readable() is False
    assert reading.exhausted() is False
    assert should_stop(reading) is False


def test_true_is_not_a_count() -> None:
    """``bool`` は ``int`` の派生なので、明示的に弾く。

    弾かないと ``True`` が残数1として読まれる。
    """
    assert reading_from_response({"remaining_count": True}).remaining is None


def test_the_evidence_names_the_keys_that_were_there() -> None:
    """読めなかったときに **次の手が決まる** ことが要件である (原則2)。

    「読めませんでした」だけでは、キー名が変わったのか型が変わったのかが
    分からない。在ったキーの名前を出す。**値は出さない。**
    """
    said = reading_from_response({"remaining": 5, "total": 62}).evidence
    assert "remaining" in said
    assert "5" not in said


# --------------------------------------------------------------------------
# 止める / 止めない
# --------------------------------------------------------------------------


@pytest.mark.parametrize("remaining", [0, -1])
def test_a_used_up_quota_stops_the_send(remaining: int) -> None:
    """**読めた上で0以下なら止める。** 負の値も枠切れとして扱う。"""
    reading = QuotaReading(remaining=remaining, total=62, evidence="観測しました")
    assert reading.exhausted() is True
    assert should_stop(reading) is True


def test_a_quota_with_room_does_not_stop_the_send() -> None:
    """**倒しすぎないこと。** 枠が在れば送る。"""
    reading = QuotaReading(remaining=1, total=62, evidence="観測しました")
    assert should_stop(reading) is False


# --------------------------------------------------------------------------
# 報告
# --------------------------------------------------------------------------


def test_the_description_shows_the_number() -> None:
    """**残数は個人データではない。** 伏せる理由が無いので値を出す (8章)。"""
    said = QuotaReading(remaining=51, total=62, evidence="観測しました").describe()
    assert "51" in said
    assert "62" in said


def test_the_description_says_so_when_it_could_not_be_read() -> None:
    said = QuotaReading(remaining=None, total=None, evidence="応答がJSONではありません").describe()
    assert "読めません" in said
    assert "応答がJSONではありません" in said
