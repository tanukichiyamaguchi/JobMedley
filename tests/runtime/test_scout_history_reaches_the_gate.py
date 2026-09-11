"""媒体のスカウト履歴が、**関門とプロンプトの両方に届く**こと。

2026-09-11、運用者が画面を見せてこう言った。

> 過去にスカウトを送信した場合はスカウト履歴が残っているはず。その場合、過去に
> スカウトしている候補者に対してのスカウト文面には、度々のご連絡失礼いたします。
> などと、初めてのスカウトではない旨を記載してほしい。
> 直近3日以内にスカウトを送っている場合はスカウト対象からは外すようにしてください。

調べたら、**問い合わせ文は 2026-08-22 の時点で既に履歴を要求していた**。応答に
載ることも観測済みだった。それでも解析側は1本も読んでおらず、プロンプトの
``{{SCOUT_HISTORY}}`` は全候補者で常に「非公開」だった。プロンプト STEP3 (3) は
「過去に送付済みの場合のみ触れる」と書いてあるので、非公開は初回へ丸め込まれ、
**3回送った相手にも初回として書かれていた。**

要求のコストだけ払って捨てる、という状態が試験で固定されてすらいた
(``test_resume_field_mapping.py`` の ``REQUIRED_FRAGMENTS``)。fragment が在ることは
検査されていたが、**読んでいることは誰も検査していなかった。**

だからこのファイルは「繋がっていること」を検査する。実測48・49回目と同じ形の
欠陥なので、同じ形の検査を置く。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobmedley_scout.api.candidates import our_job_offer_id, scout_history_from_response
from jobmedley_scout.config.loader import load_all
from jobmedley_scout.generation.facts import UNDISCLOSED
from jobmedley_scout.generation.scout_message import (
    CONTACTED_BEFORE,
    FIRST_CONTACT,
    describe_scout_history,
)
from jobmedley_scout.models.candidate import Candidate, ScoutHistoryEntry, ScoutHistorySummary
from jobmedley_scout.state.recency import scouted_within, should_skip

CONFIG = Path("config/config.yaml")
COORDINATES = Path("config/site_coordinates.yaml")
NOW = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def coordinates():  # type: ignore[no-untyped-def]
    _, co = load_all(CONFIG, COORDINATES)
    return co


def _response(*entries: dict[str, object]) -> dict[str, object]:
    return {"data": {"memberGet": {"member": {"scoutHistories": list(entries)}}}}


def _entry(offer_id: object, sent_at: str | None = None, count: int | None = None):  # type: ignore[no-untyped-def]
    return {
        "jobOffer": {"id": offer_id},
        "latestSentAt": sent_at,
        "sentCount": count,
        "latestRefusedAt": None,
    }


# --------------------------------------------------------------------------
# 座標と要求の突き合わせ
# --------------------------------------------------------------------------


def test_the_query_asks_for_the_history_and_the_coordinate_reads_it(coordinates) -> None:  # type: ignore[no-untyped-def]
    """**要求と消費が繋がっていること。**

    これが欠けていた検査である。fragment が在ることは既存の検査が見ていたが、
    その fragment の結果を読むキーパスが在るかは誰も見ていなかった。
    """
    from jobmedley_scout.api.candidates import resume_keypaths

    template = coordinates.json_path("api.resume.payload_template")
    assert "CommonScoutHistoryOnMember" in template, "問い合わせ文が履歴を要求していない"

    keypath = resume_keypaths(coordinates)["scout_histories"]
    assert keypath, "要求しているのに読むキーパスが無い (捨てている)"
    assert keypath.endswith("scoutHistories")


def test_our_own_job_offer_is_read_from_the_send_payload(coordinates) -> None:  # type: ignore[no-untyped-def]
    """自社求人IDを **別の場所へ書き写さない。**

    書き写すと、求人を切り替えたとき片方だけ古くなり、他社の履歴を自社のものと
    読み違える。読み違えれば「度々のご連絡」が嘘になる。
    """
    assert our_job_offer_id(coordinates), "送信payloadから自社求人IDを取り出せない"


# --------------------------------------------------------------------------
# 解析 -- 三値
# --------------------------------------------------------------------------


def test_other_companies_history_is_not_counted_as_ours(coordinates) -> None:  # type: ignore[no-untyped-def]
    """**他社が送った履歴で「度々のご連絡」と書かない。**

    scoutHistories[] が自社分だけを返すのか他社分も含むのかは観測していない。
    含まれていた場合に嘘を書くので、自社求人IDで絞る。
    """
    ours = our_job_offer_id(coordinates)
    from jobmedley_scout.api.candidates import resume_keypaths

    keypath = resume_keypaths(coordinates)["scout_histories"]
    summary = scout_history_from_response(
        _response(_entry("999999", "2026-09-10T00:00:00+00:00", 1)),
        keypath=keypath,
        our_offer_id=ours,
    )
    assert summary is not None
    assert not summary.contacted_before(), "他社の履歴を自社の送信として数えている"


def test_a_missing_key_is_not_read_as_no_history(coordinates) -> None:  # type: ignore[no-untyped-def]
    """応答にキーが無いことを「履歴なし」に読み替えない。

    履歴が空のときにこの媒体が ``[]`` を返すのか ``null`` を返すのかキーごと
    落とすのかを **観測していない**。分からないものを「無い」にするのが原則3が
    禁じていることである。
    """
    from jobmedley_scout.api.candidates import resume_keypaths

    keypath = resume_keypaths(coordinates)["scout_histories"]
    ours = our_job_offer_id(coordinates)
    assert scout_history_from_response({"data": {}}, keypath=keypath, our_offer_id=ours) is None


def test_an_unresolved_coordinate_means_not_observed(coordinates) -> None:  # type: ignore[no-untyped-def]
    ours = our_job_offer_id(coordinates)
    assert scout_history_from_response(_response(), keypath=None, our_offer_id=ours) is None


# --------------------------------------------------------------------------
# 関門
# --------------------------------------------------------------------------


def test_a_candidate_scouted_two_days_ago_is_excluded(coordinates) -> None:  # type: ignore[no-untyped-def]
    """運用者の要求そのもの。**直近3日以内は外す。**"""
    from jobmedley_scout.api.candidates import resume_keypaths

    summary = scout_history_from_response(
        _response(_entry(our_job_offer_id(coordinates), "2026-09-09T10:00:00+00:00", 1)),
        keypath=resume_keypaths(coordinates)["scout_histories"],
        our_offer_id=our_job_offer_id(coordinates),
    )
    assert should_skip(scouted_within(summary, now=NOW, days=3)) is True


def test_a_candidate_scouted_long_ago_still_gets_a_message(coordinates) -> None:  # type: ignore[no-untyped-def]
    """**古い履歴は除外理由ではない。** 外すのは直近だけである。

    画像で見せられた候補者は 2025/05/28 に全3回。1年以上前なので、外さずに
    「度々のご連絡」と書いて送るのが要求どおりの振る舞いである。
    """
    from jobmedley_scout.api.candidates import resume_keypaths

    summary = scout_history_from_response(
        _response(_entry(our_job_offer_id(coordinates), "2025-05-28T10:00:00+09:00", 3)),
        keypath=resume_keypaths(coordinates)["scout_histories"],
        our_offer_id=our_job_offer_id(coordinates),
    )
    assert summary is not None
    assert summary.contacted_before()
    assert should_skip(scouted_within(summary, now=NOW, days=3)) is False


# --------------------------------------------------------------------------
# プロンプトへ届くか
# --------------------------------------------------------------------------


def _candidate(history: ScoutHistorySummary | None) -> Candidate:
    return Candidate(candidate_id="1", raw_id_observed="1", scout_history=history)


def test_the_three_values_reach_the_prompt_as_three_different_strings() -> None:
    """**「観測して初回」と「観測できていない」を同じ文字列にしない。**

    同じにすると、プロンプトの分岐がどちらも初回へ落ちる。3回送った相手に
    初回として書くのが、この区別を作った理由である。
    """
    not_observed, _, _ = describe_scout_history(_candidate(None))
    first, _, _ = describe_scout_history(_candidate(ScoutHistorySummary()))
    again, _, _ = describe_scout_history(
        _candidate(ScoutHistorySummary(entries=(ScoutHistoryEntry(sent_count=3),)))
    )
    assert not_observed == UNDISCLOSED
    assert first == FIRST_CONTACT
    assert again == CONTACTED_BEFORE
    assert len({not_observed, first, again}) == 3, "三値が潰れている"


def test_the_prompt_never_receives_a_raw_timestamp() -> None:
    """**日付そのものを渡さない。**

    渡すとモデルが本文へ書き写しうる。書式を1件も観測していない以上、書き写された
    日付が正しい保証が無い。渡すのは事実と回数だけである。
    """
    _, sent_at, _ = describe_scout_history(
        _candidate(
            ScoutHistorySummary(
                entries=(ScoutHistoryEntry(latest_sent_at="2025-05-28T10:00:00+09:00"),)
            )
        )
    )
    assert "2025" not in sent_at
    assert "05-28" not in sent_at


def test_build_prompt_fills_the_history_without_being_told() -> None:
    """**呼び出し側が渡し忘れても既定の嘘にならないこと。**

    以前はこの3欄だけ呼び出し側が渡す設計で、preview も send_first も渡して
    いなかった。渡し忘れが「非公開」という既定になり、誰も選んでいない既定が
    全候補者に適用されていた。
    """
    from jobmedley_scout.generation.scout_message import build_prompt

    template = "履歴:{{SCOUT_HISTORY}} 時期:{{LAST_SENT_AT}} 反応:{{LAST_RESPONSE}}"
    filled = build_prompt(
        template,
        {},
        _candidate(ScoutHistorySummary(entries=(ScoutHistoryEntry(sent_count=3),))),
    )
    assert CONTACTED_BEFORE in filled
    assert UNDISCLOSED not in filled.split("履歴:")[1].split(" 時期:")[0]


def test_the_prompt_document_branches_three_ways() -> None:
    """プロンプト側にも3つ目の枝が在ること。

    コードが三値を渡しても、プロンプトが2値でしか分岐していなければ、非公開は
    初回へ丸め込まれる。**両側が揃って初めて繋がる。**
    """
    text = Path("config/prompts/scout_dental_hygienist.md").read_text(encoding="utf-8")
    assert "度々のご連絡失礼いたします" in text
    assert "「非公開」と書かれている場合" in text
    assert "初回だと断定する表現は使わない" in text
