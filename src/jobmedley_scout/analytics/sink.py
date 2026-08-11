"""The analytics output port.

出力先は差し替え可能にしてある。既定はローカル (:mod:`analytics.local_sink`) で、
Google Sheets は任意の追加実装。**どちらも 11.1 の列契約に従う** -- 表示列は毎回
上書き、入力列は常に空で書く。契約をシート側の実装だけに書くと、ローカル側が
「ファイルだから安全」という理由で崩し、往復ループが片方だけで再発する。

sink は **書くだけ** にしてある。集計・整形は :mod:`analytics.aggregate` と
:mod:`analytics.rewrite` に閉じているので、出力先を増やしても計算は増えない
(= 出力先ごとに数字が違う、が起きない)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from jobmedley_scout.analytics.charts import ChartAction, ChartSpec, upsert_chart_spec
from jobmedley_scout.analytics.rewrite import DisplayRow

#: 表の既定の名前。週次と月次で別の表に書き分けるために使う。
DEFAULT_TABLE: Final[str] = "weekly"


@dataclass(frozen=True)
class InputRow:
    """Human-entered values for one row, read back from the input columns.

    表示列は **含まない**。読み戻す経路に表示列が1つでも混ざると、自動の出力を
    自動が読み直す輪ができる (11.1)。型として持たせないことで塞いでいる。
    """

    row_key: str
    values: tuple[tuple[str, str], ...]

    def value(self, key: str) -> str:
        for stored_key, stored_value in self.values:
            if stored_key == key:
                return stored_value
        return ""


class AnalyticsSink(Protocol):
    """Where a rendered analytics table goes."""

    def rewrite_table(self, rows: Sequence[DisplayRow], *, table: str = DEFAULT_TABLE) -> None:
        """Replace the whole table. 差分更新ではない (10.4 と同じ理由)。"""
        ...

    def upsert_chart(self, title: str, spec: ChartSpec) -> None:
        """Create or update a chart, identified by title."""
        ...

    def read_input_columns(self, *, table: str = DEFAULT_TABLE) -> tuple[InputRow, ...]:
        """Read the human-only columns. **表示列は返さない** (11.1)。"""
        ...


@dataclass
class FakeSink:
    """Records calls instead of writing. For tests.

    実装ではなく記録を持つのは、「入力列が常に空で書かれたか」「グラフが
    タイトルで冪等か」を、ファイルもネットワークも無しで表明するため。
    """

    rewrites: list[tuple[str, tuple[DisplayRow, ...]]] = field(default_factory=list)
    chart_actions: list[tuple[ChartAction, str, ChartSpec]] = field(default_factory=list)
    #: テストが仕込む「人間が入力した値」。自動化が書いたものではない。
    input_rows: dict[str, tuple[InputRow, ...]] = field(default_factory=dict)

    def rewrite_table(self, rows: Sequence[DisplayRow], *, table: str = DEFAULT_TABLE) -> None:
        self.rewrites.append((table, tuple(rows)))

    def upsert_chart(self, title: str, spec: ChartSpec) -> None:
        decision = upsert_chart_spec(self.chart_titles(), title, spec)
        self.chart_actions.append((decision.action, decision.title, decision.spec))

    def read_input_columns(self, *, table: str = DEFAULT_TABLE) -> tuple[InputRow, ...]:
        return self.input_rows.get(table, ())

    def chart_titles(self) -> tuple[str, ...]:
        return tuple(title for _, title, _ in self.chart_actions)

    def last_rows(self, table: str = DEFAULT_TABLE) -> tuple[DisplayRow, ...]:
        for written_table, rows in reversed(self.rewrites):
            if written_table == table:
                return rows
        return ()
