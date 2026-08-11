"""11.2: 出自別の一括取り消し。「誰が書いたか」ではなく「何を根拠に書いたか」。"""

from __future__ import annotations

import pytest

from jobmedley_scout.analytics.retraction import retract_by_provenance
from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.provenance import (
    AUTO_PLATFORM_FLAG,
    AUTO_SUBJECT_MATCH,
    AUTO_SUBJECT_PREFIX,
    MANUAL_SHEET,
    Provenance,
)
from tests.analytics.helpers import make_reply


def test_retracting_a_prefix_removes_only_that_origin() -> None:
    """前方一致のロジックに欠陥が見つかったら、その根拠の行だけを消す。"""
    detections = [
        make_reply("exact-1", provenance=AUTO_SUBJECT_MATCH),
        make_reply("prefix-1", provenance=AUTO_SUBJECT_PREFIX),
        make_reply("prefix-2", provenance=AUTO_SUBJECT_PREFIX),
        make_reply("flag-1", provenance=AUTO_PLATFORM_FLAG),
        make_reply("sheet-1", provenance=MANUAL_SHEET),
    ]

    result = retract_by_provenance(detections, AUTO_SUBJECT_PREFIX)

    assert result.retracted_candidate_ids == ("prefix-1", "prefix-2")
    assert [d.candidate_id for d in result.kept] == ["exact-1", "flag-1", "sheet-1"]
    assert result.retracted_count == 2
    assert result.changed is True


def test_manual_entries_are_retractable_by_the_same_mechanism() -> None:
    """11.2: 「手動だから触れない」にしない。区分ではなく由来で選ぶ。

    参照実装はシート経由で手動扱いになった値を自動側から直せず、誤検知が
    固定化した。手動由来も他と同じ経路で一括取り消しできる必要がある。
    """
    detections = [
        make_reply("sheet-1", provenance=MANUAL_SHEET),
        make_reply("exact-1", provenance=AUTO_SUBJECT_MATCH),
    ]

    result = retract_by_provenance(detections, MANUAL_SHEET)

    assert result.retracted_candidate_ids == ("sheet-1",)


def test_detail_suffixes_do_not_change_the_origin() -> None:
    """由来は ``prefix:detail`` 形式。詳細が付いていても同じ根拠として選ばれる。"""
    detections = [
        make_reply("a", provenance=Provenance(AUTO_SUBJECT_PREFIX, "run-42").render()),
        make_reply("b", provenance=AUTO_SUBJECT_PREFIX),
    ]

    result = retract_by_provenance(detections, AUTO_SUBJECT_PREFIX)

    assert result.retracted_count == 2


def test_a_longer_prefix_is_not_swept_up() -> None:
    """接頭辞の前方一致を自前で書くと、似た名前の由来まで巻き込む。"""
    detections = [make_reply("v2", provenance=f"{AUTO_SUBJECT_PREFIX}-v2")]

    result = retract_by_provenance(detections, AUTO_SUBJECT_PREFIX)

    assert result.retracted == ()
    assert result.changed is False


def test_order_is_preserved_so_the_rewrite_stays_deterministic() -> None:
    detections = [make_reply(f"c{i}", provenance=AUTO_SUBJECT_MATCH) for i in range(5)]

    result = retract_by_provenance(detections, AUTO_SUBJECT_PREFIX)

    assert [d.candidate_id for d in result.kept] == [d.candidate_id for d in detections]


def test_an_unknown_prefix_is_refused_rather_than_silently_matching_nothing() -> None:
    """打鍵ミスは「0件取り消し」という静かな成功になる。消したつもりが消えていない。"""
    detections = [make_reply("c1", provenance=AUTO_SUBJECT_MATCH)]

    with pytest.raises(ConfigError, match="未知の由来"):
        retract_by_provenance(detections, "auto/subject_match")


def test_nothing_matched_is_reported_as_unchanged() -> None:
    detections = [make_reply("c1", provenance=AUTO_SUBJECT_MATCH)]

    result = retract_by_provenance(detections, AUTO_PLATFORM_FLAG)

    assert result.changed is False
    assert result.kept == tuple(detections)
    assert result.prefix == AUTO_PLATFORM_FLAG
