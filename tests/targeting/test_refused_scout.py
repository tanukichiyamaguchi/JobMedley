"""辞退した相手を外す。**判定は書かれていたが、誰も呼んでいなかった。**

2026-09-12、本番の送信が ``HTTP 200`` かつ ``data.result.errorMessage`` 有りで
失敗した。原因を追う途中で ``ScoutHistorySummary.refused()`` の呼び出し元が
**0件** であることが分かった。媒体から ``latestRefusedAt`` を取り込み、モデルに
``refused()`` を書き、それで終わっていた。

**部品ごとには正しく、部品のあいだが繋がっていない** -- 実測48回目・55回目と
同じ形で、これが4回目以降である。繋がっていないあいだ、この仕組みは
**スカウトを断った相手へ送ろうとする。**
"""

from __future__ import annotations

from jobmedley_scout.models.candidate import ScoutHistoryEntry, ScoutHistorySummary
from jobmedley_scout.targeting.determination import Determination
from jobmedley_scout.targeting.scout_history import (
    RULE_REFUSED_SCOUT,
    refused_scout,
    should_skip,
)


def _history(*refused_at: str | None) -> ScoutHistorySummary:
    return ScoutHistorySummary(
        entries=tuple(ScoutHistoryEntry(latest_refused_at=value) for value in refused_at)
    )


# --------------------------------------------------------------------------
# 三値
# --------------------------------------------------------------------------


def test_a_refusal_excludes_the_candidate() -> None:
    """**断った相手には送らない。**"""
    assert refused_scout(_history("2026-05-01 10:00:00")).determination is Determination.MATCH


def test_no_refusal_lets_the_candidate_through() -> None:
    """**全員を外してしまわない。** 断っていない相手は通ること。"""
    assert refused_scout(_history(None, None)).determination is Determination.NO_MATCH
    assert refused_scout(ScoutHistorySummary()).determination is Determination.NO_MATCH


def test_not_observed_is_not_the_same_as_not_refused() -> None:
    """**ここが要点である。** 未観測を「断っていない」に畳まない。

    畳むと、レジュメが読めなかった候補者が全員「断っていない」ことになり、
    断った相手へ送り始める -- 静かに、赤くもならず (原則2)。
    """
    assert refused_scout(None).determination is Determination.UNDETERMINABLE


def test_one_refusal_among_several_entries_is_enough() -> None:
    """1件でも辞退があれば外す。"""
    assert refused_scout(_history(None, "2026-05-01 10:00:00")).determination is Determination.MATCH


def test_blank_strings_do_not_count_as_a_refusal() -> None:
    """空文字・空白だけの値を辞退とみなさない。

    媒体が ``""`` を返す形は観測していないが、**在りうる形で全員を外さない**
    ようにしておく。倒しすぎれば「静かなゼロ件」になる。
    """
    assert refused_scout(_history("", "   ")).determination is Determination.NO_MATCH


# --------------------------------------------------------------------------
# 倒し方
# --------------------------------------------------------------------------


def test_undeterminable_falls_to_not_sending() -> None:
    """**判定不能は送らない側へ倒す。** 取り消せないのは送るほうである。"""
    assert should_skip(refused_scout(None)) is True
    assert should_skip(refused_scout(_history("2026-05-01 10:00:00"))) is True


def test_a_clean_history_is_not_skipped() -> None:
    assert should_skip(refused_scout(ScoutHistorySummary())) is False


def test_the_reason_is_always_written_down() -> None:
    """外した理由が空にならないこと。報告に出すのは件数ではなく理由である (原則2)。"""
    for summary in (None, _history("2026-05-01 10:00:00"), ScoutHistorySummary()):
        outcome = refused_scout(summary)
        assert outcome.evidence.strip()
        assert outcome.rule_id == RULE_REFUSED_SCOUT


def test_the_evidence_never_leaks_the_refusal_timestamp() -> None:
    """13.2: 報告に値を出さない。

    ``latestRefusedAt`` は書式を1件も観測していないので、日時としては解釈せず
    真偽としてしか使わない (原則3)。値を報告に混ぜる理由が無い。
    """
    said = refused_scout(_history("2026-05-01 10:00:00")).evidence
    assert "2026" not in said
    assert "05-01" not in said
