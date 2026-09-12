"""直近スカウトの判定。**三値であることが全部である。**

運用者の要求はこうだった。

> 直近3日以内にスカウトを送っている場合はスカウト対象からは外すようにしてください。

``bool`` で書けば3行で済む。3行で書いてはいけない理由がこのファイルである。

判定に要る ``latestSentAt`` は **書式を1件も観測していない**（2026-08-22 の観測は
値を出さない方針で、分かっているのは ``<string>`` であることだけ）。読めなかった
ときに「最近ではない」と読み替えた瞬間、**書式が想定と違う日に、直近で送った
相手へ静かに送り始める。**
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobmedley_scout.models.candidate import ScoutHistoryEntry, ScoutHistorySummary
from jobmedley_scout.state.recency import (
    describe_format,
    parse_sent_at,
    scouted_within,
    should_skip,
)
from jobmedley_scout.targeting.determination import Determination

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)


def _history(*sent_at: str | None) -> ScoutHistorySummary:
    return ScoutHistorySummary(
        entries=tuple(ScoutHistoryEntry(latest_sent_at=value) for value in sent_at)
    )


def _verdict(summary: ScoutHistorySummary | None, days: int = 3) -> Determination:
    return scouted_within(summary, now=NOW, days=days).determination


# --------------------------------------------------------------------------
# 三値
# --------------------------------------------------------------------------


def test_not_observed_is_not_the_same_as_no_history() -> None:
    """**ここが要点である。** 未観測を「履歴なし」に畳まない。

    畳むと、レジュメが読めなかった候補者が全員「初回」になる。画像で見せられた
    候補者は 2025/05/28 に全3回 送られていた -- 読めていなければ、その人に
    4回目を送ることになる。
    """
    assert _verdict(None) is Determination.UNDETERMINABLE
    assert _verdict(ScoutHistorySummary()) is Determination.NO_MATCH


def test_recent_send_excludes() -> None:
    assert _verdict(_history("2026-09-09T10:00:00+00:00")) is Determination.MATCH


def test_an_old_send_does_not_exclude() -> None:
    assert _verdict(_history("2026-01-01T10:00:00+00:00")) is Determination.NO_MATCH


def test_the_boundary_is_inclusive() -> None:
    """ちょうど3日前は「以内」に含める。

    境界をどちらに倒すかは決めの問題だが、**決めたことを検査に書く**。
    書いていないと、後から読んだ人が逆に直しても誰も気付かない。
    """
    assert _verdict(_history("2026-09-08T09:00:00+00:00")) is Determination.MATCH
    assert _verdict(_history("2026-09-08T08:59:59+00:00")) is Determination.NO_MATCH


# --------------------------------------------------------------------------
# 読めない日時 -- **ここで「最近ではない」に倒さない**
# --------------------------------------------------------------------------


def test_an_unreadable_timestamp_is_undeterminable_not_safe() -> None:
    """読めない日時を「古い」とみなさない。

    みなすと、媒体が書式を変えた日に **全員へ送り始める**。静かに、赤くもならず。
    """
    assert _verdict(_history("いつか")) is Determination.UNDETERMINABLE


def test_one_unreadable_row_poisons_an_otherwise_old_history() -> None:
    """読めた分が古くても、**読めない行が残っていれば言い切らない。**

    読めなかった行にもっと新しい送信が在りえる。「読めた分だけで判定」にすると、
    書式が混ざった瞬間にすり抜ける。
    """
    assert _verdict(_history("2026-01-01T00:00:00+00:00", "???")) is Determination.UNDETERMINABLE


def test_a_recent_readable_row_wins_over_unreadable_ones() -> None:
    """既に範囲内の送信が見つかっていれば、読めない行が残っていても結論は出る。

    外す方向には倒せる。**倒せないのは「送ってよい」の側だけ** である。
    """
    assert _verdict(_history("2026-09-10T00:00:00+00:00", "???")) is Determination.MATCH


def test_entries_without_any_timestamp_are_undeterminable() -> None:
    assert _verdict(_history(None, None)) is Determination.UNDETERMINABLE


# --------------------------------------------------------------------------
# 倒し方
# --------------------------------------------------------------------------


def test_undeterminable_falls_to_not_sending() -> None:
    """**判定不能は送らない側へ倒す。**

    取り消せないのは送るほうで、送らないのは次回やり直せる。7.1 が要求する
    「どちらへ倒すかを必ず宣言させる」の、宣言そのものがこの検査である。
    """
    assert should_skip(scouted_within(None, now=NOW, days=3)) is True
    assert should_skip(scouted_within(_history("???"), now=NOW, days=3)) is True


def test_a_clean_first_contact_is_not_skipped() -> None:
    """**全員を外してしまわない。**

    安全側へ倒す設計は、倒しすぎれば「静かなゼロ件」になる。観測できて履歴が
    無い候補者は通ること。
    """
    assert should_skip(scouted_within(ScoutHistorySummary(), now=NOW, days=3)) is False


def test_the_reason_is_always_written_down() -> None:
    """外した理由が空にならないこと。

    報告に出すのは件数ではなく理由である。「0件でした」だけの報告からは、
    次に何をすればよいかが決まらない (原則2)。
    """
    for summary in (None, _history("???"), _history("2026-09-10T00:00:00+00:00")):
        assert scouted_within(summary, now=NOW, days=3).evidence.strip()


# --------------------------------------------------------------------------
# 書式
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2026-09-10T12:00:00+09:00",
        "2026-09-10T12:00:00Z",
        "2026-09-10T12:00:00",
        "2026-09-10 12:00:00",
        "2026-09-10",
        "2026/09/10",
        "2026年9月10日",
    ],
)
def test_plausible_formats_are_read(raw: str) -> None:
    """**寛容に読む** (7.7)。書式を観測していないので、ありうる形を試す。"""
    assert parse_sent_at(raw) is not None


@pytest.mark.parametrize("raw", [None, "", "   ", "昨日", "2026-13-45"])
def test_impossible_values_are_not_invented(raw: str | None) -> None:
    """**読めないものを読めたことにしない。** 推測で日付を作らない (原則3)。"""
    assert parse_sent_at(raw) is None


def test_every_parsed_moment_carries_a_timezone() -> None:
    """素朴な datetime を返さない。

    タイムゾーン付きと素朴な datetime を比較すると ``TypeError`` になる。
    判定の只中で落ちるので、入口で揃える。
    """
    for raw in ("2026-09-10", "2026-09-10T12:00:00", "2026/09/10"):
        parsed = parse_sent_at(raw)
        assert parsed is not None
        assert parsed.tzinfo is not None


def test_the_format_description_never_leaks_the_value() -> None:
    """13.2: 報告に値を出さない。種別だけを出す。"""
    said = describe_format("2026-09-10T12:34:56+09:00")
    assert "2026" not in said
    assert "34" not in said


def test_a_negative_window_is_refused() -> None:
    """負の日数は設定の打鍵ミスである。**黙って通さない。**"""
    with pytest.raises(ValueError, match="0 以上"):
        scouted_within(ScoutHistorySummary(), now=NOW, days=-1)


# --------------------------------------------------------------------------
# 書式を、値を出さずに写す
# --------------------------------------------------------------------------


def test_the_shape_masks_every_digit() -> None:
    """**値ではなく並びを出す。**

    2026-09-12 実測54回目、段階5 の通しで5名全員が外れた。報告に出ていたのは
    「読めない書式 (長さ 19)」だけで、**長さ19 の候補は複数あった**。ISO の2形は
    既に読めていたので、残りを当てるしかなくなった。

    当てるくらいなら形を出せばよい。**値を出さずに形を出す方法はある。**
    """
    from jobmedley_scout.state.recency import shape_of

    assert shape_of("2025/05/28 10:00:00") == "NNNN/NN/NN NN:NN:NN"
    assert shape_of("2025-05-28T10:00:00+09:00") == "NNNN-NN-NNTNN:NN:NN+NN:NN"


def test_the_shape_never_carries_a_digit_or_a_word() -> None:
    """13.2: 並びに値が混ざらないこと。

    数字は ``N``、``T``/``Z`` 以外の英字は ``A`` になる。日本語も英字として
    扱われるので、文言がそのまま出ることはない。
    """
    from jobmedley_scout.state.recency import shape_of

    for raw in ("2025/05/28 10:00:00", "2025年5月28日", "28 May 2025", "令和7年5月28日"):
        shape = shape_of(raw)
        assert not any(char.isdigit() for char in shape), shape
        assert all(char in "NA" or not char.isalpha() for char in shape), shape


def test_the_description_shows_the_shape_not_just_the_length() -> None:
    """「長さ 19」では次の手が決まらない。**並びまで出す。**"""
    said = describe_format("2025/05/28 10:00:00")
    assert "NNNN/NN/NN NN:NN:NN" in said
    assert "2025" not in said


# --------------------------------------------------------------------------
# 実測54回目の書式
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2025/05/28 10:00:00",
        "2025/05/28T10:00:00",
        "2025/05/28 10:00",
        "2025.05.28 10:00:00",
    ],
)
def test_slash_separated_datetimes_are_read(raw: str) -> None:
    """スラッシュ区切りの日時を読めること。

    **これは仮説である。** 段階5 の通しで5名全員が「読めない書式 (長さ 19)」で
    外れ、長さ19 の ISO 形は既に読めていた。画面には ``送信日:2025/05/28`` と
    出ているのでスラッシュ区切りが有力だが、**観測したわけではない**。

    外れていても静かには失敗しない -- :func:`describe_format` が並びを出すので、
    次の実行で正体が分かる。
    """
    assert parse_sent_at(raw) is not None
