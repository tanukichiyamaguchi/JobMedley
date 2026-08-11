"""Chart upsert decisions -- pure, idempotent by title.

**タイトルで既存を特定し、あれば更新・なければ作成。** 毎回作成すると、実行のたびに
同じグラフが積み上がり、数週間で誰も開かないシートになる。ID を持たない出力先
(シートのグラフ、Markdown の節) でも一意に決まる鍵はタイトルしかない。

判断だけを行い、実際の作成・更新は sink が行う (:mod:`analytics.sink`)。分けて
あるのは、「どちらの操作になるか」をI/O無しでテストできるようにするため。

:class:`ChartSpec` は列を **キー** で受け取り、位置は
:func:`~analytics.sheet_schema.column_index` から引く。グラフが参照する位置と
実際に書かれる列が同じ情報源から出るので、片方だけずれる事故が起きない。
入力列を参照するグラフは拒否する -- 人間の手入力がグラフに現れると、11.1 の
往復ループが「見た目」経由で再発する。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator

from jobmedley_scout.analytics.sheet_schema import column_index, is_input_column, role_of
from jobmedley_scout.errors import ConfigError

_STRICT = ConfigDict(extra="forbid", frozen=True)


class ChartKind(StrEnum):
    LINE = "line"
    COLUMN = "column"


class ChartAction(StrEnum):
    """What the sink must do for one chart."""

    CREATE = "create"
    UPDATE = "update"


class ChartSpec(BaseModel):
    """A chart described by column keys, resolved to fixed positions on demand."""

    model_config = _STRICT

    kind: ChartKind
    x_column: str
    y_columns: tuple[str, ...]
    y_axis_label: str

    @model_validator(mode="after")
    def _columns_are_display_columns(self) -> ChartSpec:
        if not self.y_columns:
            raise ValueError("グラフには1つ以上の系列が必要です")
        for key in (self.x_column, *self.y_columns):
            # 未知のキーはここで落とす。column_index が ConfigError を投げるので
            # 「グラフだけ静かに空になる」状態にはならない。
            role_of(key)
            if is_input_column(key):
                # 11.1: 入力列をグラフに出すと、人間の手入力が自動出力の一部として
                # 表示され、次に見た人がそれを自動判定だと読む。輪はここでも閉じる。
                raise ValueError(f"入力列 {key!r} はグラフに使えません (11.1)")
        if self.x_column in self.y_columns:
            raise ValueError(f"横軸 {self.x_column!r} を系列にも指定しています")
        if len(set(self.y_columns)) != len(self.y_columns):
            raise ValueError(f"系列に重複があります: {self.y_columns}")
        return self

    def x_position(self) -> int:
        return column_index(self.x_column)

    def y_positions(self) -> tuple[int, ...]:
        return tuple(column_index(key) for key in self.y_columns)


@dataclass(frozen=True)
class ChartDecision:
    """What to do, and with what. ``action`` is the :class:`ChartAction`.

    真偽値ではなく結果オブジェクトを返すのは、呼び出し側が「なぜ更新なのか」
    (= そのタイトルが既にあったこと) をログに出せるようにするため。
    """

    action: ChartAction
    title: str
    spec: ChartSpec


def upsert_chart_spec(
    existing_titles: Iterable[str],
    title: str,
    spec: ChartSpec,
) -> ChartDecision:
    """CREATE when the title is new, UPDATE when it already exists.

    比較はタイトルの完全一致。正規化して寄せない -- 「返信率(週次)」と
    「返信率 (週次)」を同一視すると、意図して分けた2枚が片方だけ残る。
    """
    if not title.strip():
        # 空タイトルは全部が同一グラフに畳まれるか、毎回新規作成されるかの
        # どちらかになる。どちらも黙って壊れるので、先に落とす。
        raise ConfigError("グラフのタイトルが空です")
    known = tuple(existing_titles)
    action = ChartAction.UPDATE if title in known else ChartAction.CREATE
    return ChartDecision(action=action, title=title, spec=spec)


#: 既定のグラフ。タイトルは **鍵** なので、変えると古いグラフが残って新しいのが
#: 増える。文言を直したくなったら、古いタイトルの削除もセットで考えること。
WEEKLY_REPLY_RATE_TITLE: Final[str] = "週次 返信率"
WEEKLY_SLOT_VOLUME_TITLE: Final[str] = "週次 送信数 (枠別)"
MONTHLY_REPLY_RATE_TITLE: Final[str] = "月次 返信率"


def default_chart_specs() -> tuple[tuple[str, ChartSpec], ...]:
    """The charts the analytics step keeps up to date."""
    return (
        (
            WEEKLY_REPLY_RATE_TITLE,
            ChartSpec(
                kind=ChartKind.LINE,
                x_column="cohort",
                y_columns=("reply_rate_pct", "free_rate_pct", "paid_rate_pct"),
                y_axis_label="返信率(%)",
            ),
        ),
        (
            WEEKLY_SLOT_VOLUME_TITLE,
            ChartSpec(
                kind=ChartKind.COLUMN,
                x_column="cohort",
                # 9.4: 不明枠も必ず描く。描かないと「合計と内訳が合わない」
                # グラフになり、記録漏れが視覚的に隠れる。
                y_columns=("free_sent", "paid_sent", "unknown_sent"),
                y_axis_label="送信数",
            ),
        ),
        (
            MONTHLY_REPLY_RATE_TITLE,
            ChartSpec(
                kind=ChartKind.LINE,
                x_column="cohort",
                y_columns=("reply_rate_pct",),
                y_axis_label="返信率(%)",
            ),
        ),
    )
