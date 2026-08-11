"""グラフはタイトルで冪等。毎回作成すると、数週間で誰も開かないシートになる。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobmedley_scout.analytics.charts import (
    ChartAction,
    ChartKind,
    ChartSpec,
    default_chart_specs,
    upsert_chart_spec,
)
from jobmedley_scout.analytics.sheet_schema import column_index
from jobmedley_scout.errors import ConfigError


def _spec() -> ChartSpec:
    return ChartSpec(
        kind=ChartKind.LINE,
        x_column="cohort",
        y_columns=("reply_rate_pct",),
        y_axis_label="返信率(%)",
    )


def test_existing_title_yields_update() -> None:
    decision = upsert_chart_spec(("週次 返信率", "月次 返信率"), "週次 返信率", _spec())

    assert decision.action is ChartAction.UPDATE
    assert decision.title == "週次 返信率"


def test_new_title_yields_create() -> None:
    decision = upsert_chart_spec(("月次 返信率",), "週次 返信率", _spec())

    assert decision.action is ChartAction.CREATE


def test_upsert_is_idempotent_when_applied_repeatedly() -> None:
    """2回目以降はずっと UPDATE。同じグラフが積み上がらない。"""
    titles: list[str] = []
    for _ in range(3):
        decision = upsert_chart_spec(titles, "週次 返信率", _spec())
        if decision.action is ChartAction.CREATE:
            titles.append(decision.title)

    assert titles == ["週次 返信率"]
    assert upsert_chart_spec(titles, "週次 返信率", _spec()).action is ChartAction.UPDATE


def test_titles_are_not_normalized_before_comparison() -> None:
    """「返信率(週次)」と「返信率 (週次)」を同一視すると、分けた2枚が片方だけ残る。"""
    decision = upsert_chart_spec(("週次 返信率",), "週次　返信率", _spec())

    assert decision.action is ChartAction.CREATE


def test_empty_title_is_refused() -> None:
    with pytest.raises(ConfigError):
        upsert_chart_spec((), "   ", _spec())


def test_chart_columns_resolve_to_fixed_positions() -> None:
    """グラフが参照する位置と、実際に書かれる列が同じ情報源から出ること。"""
    spec = ChartSpec(
        kind=ChartKind.COLUMN,
        x_column="cohort",
        y_columns=("free_sent", "paid_sent", "unknown_sent"),
        y_axis_label="送信数",
    )

    assert spec.x_position() == column_index("cohort")
    assert spec.y_positions() == (
        column_index("free_sent"),
        column_index("paid_sent"),
        column_index("unknown_sent"),
    )


def test_a_chart_may_not_reference_an_input_column() -> None:
    """11.1: 人間の手入力がグラフに出ると、次に見た人が自動判定だと読む。"""
    with pytest.raises(ValidationError, match="11.1"):
        ChartSpec(
            kind=ChartKind.LINE,
            x_column="cohort",
            y_columns=("manual_note",),
            y_axis_label="メモ",
        )


def test_unknown_columns_are_refused() -> None:
    with pytest.raises(ValidationError):
        ChartSpec(
            kind=ChartKind.LINE,
            x_column="cohort",
            y_columns=("does_not_exist",),
            y_axis_label="?",
        )


def test_empty_series_is_refused() -> None:
    with pytest.raises(ValidationError):
        ChartSpec(kind=ChartKind.LINE, x_column="cohort", y_columns=(), y_axis_label="?")


def test_default_specs_have_unique_titles_and_valid_columns() -> None:
    specs = default_chart_specs()
    titles = [title for title, _ in specs]

    assert len(set(titles)) == len(titles)
    for _, spec in specs:
        assert spec.y_positions()


def test_default_slot_chart_includes_the_unknown_slot() -> None:
    """9.4: 不明枠を描かないと、記録漏れが視覚的に隠れる。"""
    by_title = dict(default_chart_specs())

    assert "unknown_sent" in by_title["週次 送信数 (枠別)"].y_columns
