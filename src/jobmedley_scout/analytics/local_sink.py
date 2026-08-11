"""The default sink: CSV + a Markdown summary under ``AnalyticsConfig.output_dir``.

**このモジュールだけはファイルI/Oを行う。** 計算は :mod:`analytics.aggregate` と
:mod:`analytics.rewrite` に閉じており、ここは受け取った行をそのまま書くだけの
「馬鹿な書き手」である。出力先を増やしたときに数字が食い違わないのはこの分離の
おかげなので、ここに集計を持ち込まないこと。

11.1 の列契約はローカルでも同じように守る:

- 表 (``<table>.csv``) は毎回 **全量書き換え**。入力列の位置には空セルを書く。
- 人間の入力は ``<table>_input.csv`` という **別ファイル** に置く。自動化は
  行キーの追加しかしない。既存行は読むだけで、書き戻さない。

入力を別ファイルにしたのは、全量書き換えが毎回入力列を空にするからである。同じ
ファイルに置くと、人間が書いた翌朝の実行で消える。シートでは列で分ければ足りるが
(表示列の範囲だけを書けばよい)、CSV は行単位でしか書けないので、同じ契約を満たす
には物理的に別ファイルにする必要がある。**契約は同じ、実現手段が違う。**

出力には時刻を書かない。生成時刻を1行入れるだけで、内容が同じでも毎回差分になり、
「先週から何が変わったか」を diff で追えなくなる。
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from jobmedley_scout.analytics.charts import ChartAction, ChartSpec, upsert_chart_spec
from jobmedley_scout.analytics.rewrite import DisplayRow
from jobmedley_scout.analytics.sheet_schema import (
    DISPLAY_COLUMNS,
    INPUT_COLUMNS,
    ROW_KEY_COLUMN,
    blank_input_cell,
    header_row,
    input_keys,
)
from jobmedley_scout.analytics.sink import DEFAULT_TABLE, InputRow

#: CSV の改行は明示的に固定する。既定はプラットフォーム依存 (``\r\n``) なので、
#: 環境が違うだけで全行が差分になる。
_LINE_TERMINATOR = "\n"


@dataclass
class LocalSink:
    """Writes CSV and Markdown. Satisfies :class:`~analytics.sink.AnalyticsSink`."""

    output_dir: Path
    #: タイトル → 仕様。CSV/Markdown には図を描けないので、仕様を一覧として残す。
    _charts: dict[str, ChartSpec] = field(default_factory=dict)
    #: 実行ログ用。何が新規で何が更新だったかを呼び出し側が出せるようにする。
    chart_actions: list[tuple[ChartAction, str]] = field(default_factory=list)

    def rewrite_table(self, rows: Sequence[DisplayRow], *, table: str = DEFAULT_TABLE) -> None:
        """Replace the table files wholesale."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._write_csv(table, rows)
        self._write_markdown(table, rows)
        # 入力ファイルは全量書き換えの対象外。行キーの追加だけ行う。
        self._extend_input_template(table, tuple(row.row_key for row in rows))

    def upsert_chart(self, title: str, spec: ChartSpec) -> None:
        """Record a chart spec, creating or updating by title.

        作成か更新かの判断は :func:`~analytics.charts.upsert_chart_spec` に任せる。
        sink は決めない -- 決めさせると出力先ごとに冪等性の実装が分かれ、片方だけ
        毎回新規作成する状態になる。
        """
        decision = upsert_chart_spec(tuple(self._charts), title, spec)
        self._charts[decision.title] = decision.spec
        self.chart_actions.append((decision.action, decision.title))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._write_charts_markdown()

    def read_input_columns(self, *, table: str = DEFAULT_TABLE) -> tuple[InputRow, ...]:
        """Read the human-only file. Returns ``()`` when it does not exist yet.

        表示列は読まない。読み戻す対象に自動の出力が混ざった瞬間、取り消した
        誤検知が「手入力」として復活する経路ができる (11.1)。
        """
        path = self.input_path(table)
        if not path.exists():
            return ()
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None:
                return ()
            positions = self._input_positions(header)
            rows: list[InputRow] = []
            for record in reader:
                if not record:
                    continue
                rows.append(
                    InputRow(
                        row_key=record[0],
                        values=tuple(
                            (key, record[index] if index < len(record) else "")
                            for key, index in positions
                        ),
                    )
                )
        return tuple(rows)

    def table_path(self, table: str = DEFAULT_TABLE) -> Path:
        return self.output_dir / f"{table}.csv"

    def markdown_path(self, table: str = DEFAULT_TABLE) -> Path:
        return self.output_dir / f"{table}.md"

    def input_path(self, table: str = DEFAULT_TABLE) -> Path:
        return self.output_dir / f"{table}_input.csv"

    def charts_path(self) -> Path:
        return self.output_dir / "charts.md"

    # -- writers ---------------------------------------------------------

    def _write_csv(self, table: str, rows: Sequence[DisplayRow]) -> None:
        with self.table_path(table).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator=_LINE_TERMINATOR)
            writer.writerow(header_row())
            for row in rows:
                # cells() が入力列の位置に空セルを入れる。ここで行を組み立て直さ
                # ないこと -- 組み立てを2箇所に持つと、片方だけ契約を破る。
                writer.writerow(row.cells())

    def _write_markdown(self, table: str, rows: Sequence[DisplayRow]) -> None:
        headers = [column.header for column in DISPLAY_COLUMNS]
        lines = [
            f"# {table}",
            "",
            "| " + " | ".join(headers) + " |",
            "|" + "|".join(["---"] * len(headers)) + "|",
        ]
        for row in rows:
            # 表示列だけを出す。入力列は人間専用のファイル側にあり、ここに空欄を
            # 並べても読み手を混乱させるだけ (自動化が値を書かない点は同じ)。
            lines.append("| " + " | ".join(row.value(c.key) for c in DISPLAY_COLUMNS) + " |")
        lines.extend(
            [
                "",
                "空欄の返信率は「0%」ではなく **算出不能** です "
                "(送信0件、または母数不足)。既定値に寄せていません。",
                "",
                f"手入力は `{self.input_path(table).name}` に書いてください。"
                "この表は毎回全量書き換えされるため、ここへの記入は次回の実行で消えます (11.1)。",
                "",
            ]
        )
        self.markdown_path(table).write_text("\n".join(lines), encoding="utf-8")

    def _write_charts_markdown(self) -> None:
        lines = ["# charts", ""]
        for title in sorted(self._charts):
            spec = self._charts[title]
            lines.extend(
                [
                    f"## {title}",
                    "",
                    f"- 種類: {spec.kind.value}",
                    f"- 横軸: {spec.x_column} (列位置 {spec.x_position()})",
                    f"- 系列: {', '.join(spec.y_columns)} (列位置 {list(spec.y_positions())})",
                    f"- 縦軸ラベル: {spec.y_axis_label}",
                    "",
                ]
            )
        self.charts_path().write_text("\n".join(lines), encoding="utf-8")

    def _extend_input_template(self, table: str, row_keys: Sequence[str]) -> None:
        """Append missing row keys to the human-owned file. Never rewrite it.

        既存行には一切触らない。「ついでに整形する」「並べ替える」を足した瞬間に
        11.1 の往復ループが再発する -- 自動化が入力欄に書き込む経路ができるため。
        追加するのは行キーと **空の** 入力セルだけ。
        """
        path = self.input_path(table)
        existing: list[str] = []
        if path.exists():
            with path.open(encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                next(reader, None)
                existing = [record[0] for record in reader if record]

        known = set(existing)
        missing = [key for key in row_keys if key not in known]
        if path.exists() and not missing:
            return

        mode = "a" if path.exists() else "w"
        with path.open(mode, encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator=_LINE_TERMINATOR)
            if mode == "w":
                writer.writerow([ROW_KEY_COLUMN, *(column.header for column in INPUT_COLUMNS)])
            for key in missing:
                writer.writerow([key, *(blank_input_cell() for _ in INPUT_COLUMNS)])

    def _input_positions(self, header: Sequence[str]) -> tuple[tuple[str, int], ...]:
        """Map input column keys to their position in the file's header.

        ヘッダ名で引く。位置を決め打ちすると、人間が列を1つ挿しただけで別の列を
        読む (しかもエラーは出ない)。見つからない列は空文字として扱う。
        """
        positions: list[tuple[str, int]] = []
        for key, column in zip(input_keys(), INPUT_COLUMNS, strict=True):
            try:
                positions.append((key, list(header).index(column.header)))
            except ValueError:
                positions.append((key, len(header)))
        return tuple(positions)
