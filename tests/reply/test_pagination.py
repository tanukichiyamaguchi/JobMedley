"""10.5: 終了判定はページの内容だけで行う。DOMのページャは当てにしない。"""

from __future__ import annotations

import inspect

from jobmedley_scout.reply import pagination
from jobmedley_scout.reply.extract import InboxRow, RowSource
from jobmedley_scout.reply.pagination import (
    EMPTY_SIGNATURE,
    PageDecision,
    decide_pagination,
    keeps_rows,
    marker_signature,
    page_signature,
    stop_reason,
)


def make_row(row_index: int, subject: str) -> InboxRow:
    return InboxRow(
        row_index=row_index,
        source=RowSource.JSON_PATH,
        subject=subject,
        subject_norm=subject,
        fields=(("subject", subject),),
    )


def test_signature_is_stable_and_order_independent() -> None:
    """行の並びは媒体側の都合で揺れる。順序で署名すると同じページを別物と誤認する。"""
    rows = [make_row(0, "件名A"), make_row(1, "件名B")]
    reordered = [make_row(0, "件名B"), make_row(1, "件名A")]

    assert page_signature(rows) == page_signature(reordered)
    assert page_signature(rows) != page_signature([make_row(0, "件名A")])


def test_signature_does_not_leak_the_markers() -> None:
    """署名はログとDBに残る。件名や氏名を平文で残さない (13.2)。"""
    signature = page_signature([make_row(0, "田中太郎様|介護のお仕事のご紹介|8/11")])

    assert signature.isalnum()
    assert signature.isascii()
    assert "田中" not in signature


def test_a_page_with_no_markers_is_past_the_end_and_its_rows_are_discarded() -> None:
    signature = page_signature([])

    assert signature == EMPTY_SIGNATURE
    decision = decide_pagination(signature, [])
    assert decision is PageDecision.PAST_END
    # 空ページの行を収集結果に混ぜない。分母が壊れる。
    assert keeps_rows(decision) is False
    assert stop_reason(decision) == "past_end"


def test_rows_without_any_recoverable_marker_also_read_as_past_the_end() -> None:
    blank = InboxRow(
        row_index=0,
        source=RowSource.STRUCTURAL_SCAN,
        subject=None,
        subject_norm=None,
        fields=(),
    )

    assert page_signature([blank]) == EMPTY_SIGNATURE


def test_a_repeated_signature_stops_the_run() -> None:
    first = page_signature([make_row(0, "件名A")])
    second = page_signature([make_row(0, "件名B")])

    assert decide_pagination(second, [first]) is PageDecision.CONTINUE
    decision = decide_pagination(second, [first, second])
    assert decision is PageDecision.NOT_ADVANCING
    # 取り込み済みのページの再取得なので、こちらも行を捨てる。
    assert keeps_rows(decision) is False
    assert stop_reason(decision) == "not_advancing"


def test_a_signature_seen_earlier_than_the_previous_page_also_stops() -> None:
    """ページャが1ページ目に戻り続けると、直前だけを見ていては終わらない。"""
    first = page_signature([make_row(0, "件名A")])
    second = page_signature([make_row(0, "件名B")])

    assert decide_pagination(first, [first, second]) is PageDecision.NOT_ADVANCING


def test_a_fresh_page_continues_and_keeps_its_rows() -> None:
    decision = decide_pagination(page_signature([make_row(0, "件名C")]), [])

    assert decision is PageDecision.CONTINUE
    assert keeps_rows(decision) is True
    assert stop_reason(decision) is None


def test_decide_pagination_has_no_next_link_dependency() -> None:
    """SPAではページャが初期DOMに無い。「次ページ要素があるか」で止めた実装は
    1ページ目で必ず停止し、過去週の返信率を恒久的に0で固定した (10.5)。
    この分岐が無いのは書き忘れではないので、引数が増えていないことを固定する。
    """
    parameters = inspect.signature(decide_pagination).parameters
    assert list(parameters) == ["signature", "history"]

    forbidden = ("next", "link", "pager", "has_more", "button", "selector", "dom")
    offending = [
        name for name in dir(pagination) if any(word in name.lower() for word in forbidden)
    ]
    assert offending == []


def test_marker_signature_ignores_blank_markers() -> None:
    assert marker_signature(["", "   ", "件名A"]) == marker_signature(["件名A"])
