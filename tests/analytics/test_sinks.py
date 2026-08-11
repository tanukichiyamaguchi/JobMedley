"""既定の sink (ローカル) と FakeSink が、同じ 11.1 の列契約に従うことを表明する。"""

from __future__ import annotations

import csv
from pathlib import Path

from jobmedley_scout.analytics.charts import ChartAction, ChartKind, ChartSpec
from jobmedley_scout.analytics.local_sink import LocalSink
from jobmedley_scout.analytics.rewrite import DisplayRow, build_rows
from jobmedley_scout.analytics.sheet_schema import (
    INPUT_COLUMNS,
    column_index,
    column_order,
    header_row,
    input_keys,
)
from jobmedley_scout.analytics.sink import AnalyticsSink, FakeSink, InputRow
from jobmedley_scout.models.send_record import SendSlot
from tests.analytics.helpers import jst, make_clock, make_config, make_reply, make_send


def _sample_rows() -> tuple[DisplayRow, ...]:
    sends = [
        make_send("c1", jst(2026, 7, 27), slot=SendSlot.FREE, record_id=1),
        make_send("c2", jst(2026, 8, 3), slot=SendSlot.PAID, record_id=2),
        make_send("c3", jst(2026, 8, 3), slot=SendSlot.UNKNOWN, record_id=3),
    ]
    return build_rows(sends, [make_reply("c2")], make_config(), make_clock(jst(2026, 8, 11)))


def _chart() -> ChartSpec:
    return ChartSpec(
        kind=ChartKind.LINE,
        x_column="cohort",
        y_columns=("reply_rate_pct",),
        y_axis_label="返信率(%)",
    )


def test_both_sinks_satisfy_the_protocol(tmp_path: Path) -> None:
    local: AnalyticsSink = LocalSink(output_dir=tmp_path)
    fake: AnalyticsSink = FakeSink()

    assert local is not fake


def test_local_sink_writes_input_columns_empty(tmp_path: Path) -> None:
    """11.1: 自動化が入力列に書いてよい値は空文字だけ。"""
    sink = LocalSink(output_dir=tmp_path)

    sink.rewrite_table(_sample_rows())

    with sink.table_path().open(encoding="utf-8", newline="") as handle:
        records = list(csv.reader(handle))

    assert records[0] == list(header_row())
    assert len(records) == 3  # header + 2 cohorts
    for record in records[1:]:
        assert len(record) == len(column_order())
        for key in input_keys():
            assert record[column_index(key)] == ""


def test_local_sink_rewrite_is_byte_identical_across_two_runs(tmp_path: Path) -> None:
    """全量書き換えは冪等。時刻を1つ混ぜるだけで毎回全行が差分になる。"""
    sink = LocalSink(output_dir=tmp_path)
    rows = _sample_rows()

    sink.rewrite_table(rows)
    first_csv = sink.table_path().read_bytes()
    first_md = sink.markdown_path().read_bytes()

    sink.rewrite_table(rows)

    assert sink.table_path().read_bytes() == first_csv
    assert sink.markdown_path().read_bytes() == first_md


def test_local_sink_shrinks_the_table_when_rows_disappear(tmp_path: Path) -> None:
    """差分更新だと、集計バグを直しても古い行が残る (10.4 と同じ理屈)。"""
    sink = LocalSink(output_dir=tmp_path)
    sink.rewrite_table(_sample_rows())

    sink.rewrite_table(())

    with sink.table_path().open(encoding="utf-8", newline="") as handle:
        records = list(csv.reader(handle))

    assert records == [list(header_row())]


def test_human_input_survives_the_next_rewrite(tmp_path: Path) -> None:
    """入力は別ファイル。全量書き換えが人間の記入を消してはならない。"""
    sink = LocalSink(output_dir=tmp_path)
    sink.rewrite_table(_sample_rows())

    # 人間が入力ファイルに記入する。
    path = sink.input_path()
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("2026-W32,,", "2026-W32,要確認,誤検知の疑い"), encoding="utf-8")

    sink.rewrite_table(_sample_rows())
    read_back = {row.row_key: row for row in sink.read_input_columns()}

    assert read_back["2026-W32"].value("manual_review") == "要確認"
    assert read_back["2026-W32"].value("manual_note") == "誤検知の疑い"


def test_read_input_columns_never_returns_display_values(tmp_path: Path) -> None:
    """読み戻す対象に自動の出力が混ざると、取り消した誤検知が復活する経路ができる。"""
    sink = LocalSink(output_dir=tmp_path)
    sink.rewrite_table(_sample_rows())

    for row in sink.read_input_columns():
        assert [key for key, _ in row.values] == list(input_keys())


def test_new_cohorts_are_appended_to_the_input_file_without_touching_old_rows(
    tmp_path: Path,
) -> None:
    sink = LocalSink(output_dir=tmp_path)
    sends = [make_send("c1", jst(2026, 7, 27), record_id=1)]
    config, clock = make_config(), make_clock(jst(2026, 8, 11))
    sink.rewrite_table(build_rows(sends, [], config, clock))

    path = sink.input_path()
    path.write_text(
        path.read_text(encoding="utf-8").replace("2026-W31,,", "2026-W31,済,見た"),
        encoding="utf-8",
    )

    sends.append(make_send("c2", jst(2026, 8, 3), record_id=2))
    sink.rewrite_table(build_rows(sends, [], config, clock))
    read_back = {row.row_key: row for row in sink.read_input_columns()}

    assert read_back["2026-W31"].value("manual_review") == "済"
    assert read_back["2026-W32"].value("manual_review") == ""


def test_read_input_columns_is_empty_before_anything_is_written(tmp_path: Path) -> None:
    assert LocalSink(output_dir=tmp_path).read_input_columns() == ()


def test_input_columns_are_located_by_header_not_by_position(tmp_path: Path) -> None:
    """人間が列を1つ挿しただけで別の列を読む、という壊れ方をしないこと。"""
    sink = LocalSink(output_dir=tmp_path)
    sink.rewrite_table(_sample_rows())

    path = sink.input_path()
    records = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    shifted = [[record[0], "人間が足した列", *record[1:]] for record in records]
    shifted[0][1] = "自由記入"
    shifted[1][2] = "要確認"
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(shifted)

    read_back = {row.row_key: row for row in sink.read_input_columns()}

    assert read_back["2026-W31"].value("manual_review") == "要確認"


def test_local_sink_charts_are_idempotent_by_title(tmp_path: Path) -> None:
    sink = LocalSink(output_dir=tmp_path)

    sink.upsert_chart("週次 返信率", _chart())
    first = sink.charts_path().read_text(encoding="utf-8")
    sink.upsert_chart("週次 返信率", _chart())

    assert sink.charts_path().read_text(encoding="utf-8") == first
    assert [action for action, _ in sink.chart_actions] == [
        ChartAction.CREATE,
        ChartAction.UPDATE,
    ]


def test_markdown_summary_explains_that_blank_is_not_zero(tmp_path: Path) -> None:
    sink = LocalSink(output_dir=tmp_path)

    sink.rewrite_table(_sample_rows())
    text = sink.markdown_path().read_text(encoding="utf-8")

    assert "算出不能" in text
    assert sink.input_path().name in text


def test_fake_sink_records_calls() -> None:
    sink = FakeSink()
    rows = _sample_rows()

    sink.rewrite_table(rows)
    sink.upsert_chart("週次 返信率", _chart())
    sink.upsert_chart("週次 返信率", _chart())

    assert sink.last_rows() == rows
    assert [action for action, _, _ in sink.chart_actions] == [
        ChartAction.CREATE,
        ChartAction.UPDATE,
    ]


def test_fake_sink_returns_only_what_a_human_entered() -> None:
    sink = FakeSink()
    sink.input_rows["weekly"] = (InputRow("2026-W32", (("manual_review", "要確認"),)),)

    assert sink.read_input_columns()[0].value("manual_review") == "要確認"
    assert sink.read_input_columns()[0].value("sent") == ""


def test_input_columns_exist_at_all() -> None:
    """入力列を全部消すと、人間は表示列に書くしかなくなる (11.1 の入り口)。"""
    assert INPUT_COLUMNS
