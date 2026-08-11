"""The shared column contract: display columns vs input columns (11.1, 11.2).

**自動判定の出力列に人間が手入力できる状態にすると、往復ループでデータが汚染される。**

参照実装の事故はこうだった。自動で誤検知を取り消した直後、**同じ実行の次の段階**
で、その行がシート経由の「手入力」として読み戻され、取り消したはずの誤検知が復活
した。出力列と入力列が同じ列だったからである。自動が書く → 人間が直す →
自動が読み戻す → 自動の判定として書き戻す、という輪が閉じていた。

対処は列レベルの物理分離であり、**フラグやタイムスタンプでは不十分**である。
「この値は自動が書いた」という印を付けても、その印を信じて読み戻す経路が1本でも
残っていれば輪は閉じる。分離すべきは値ではなく置き場所である。

- :data:`DISPLAY_COLUMNS` -- DBの射影。毎回の全量書き換えで上書きされる。
  **絶対に読み戻さない。** ここに書かれた値の唯一の出所はDBである。
- :data:`INPUT_COLUMNS` -- 人間専用。自動化は **常に空で書く**。ここに値が
  あるなら、それは人間が入れたものだと構造的に断言できる。

構造で担保している点が2つある:

1. :class:`~jobmedley_scout.analytics.rewrite.DisplayRow` には入力列の値を持つ
   フィールドが **存在しない**。計算値を入力列に流し込む経路がコード上に無い。
2. 列の追加は常に :data:`ALL_COLUMNS` の **末尾** に足す。グラフは列位置を固定で
   参照するため、並べ替えると全グラフの参照先がずれる。役割で並べ直したくなるが、
   やってはいけない (:func:`column_order` のテストが位置を固定している)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from jobmedley_scout.errors import ConfigError
from jobmedley_scout.models.send_record import SendSlot


class ColumnRole(StrEnum):
    """Who is allowed to write a column."""

    #: 自動化が毎回上書きする。人間が書いても次の実行で消える。
    DISPLAY = "display"
    #: 人間専用。自動化は空文字しか書かない。
    INPUT = "input"


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    role: ColumnRole


@dataclass(frozen=True)
class SlotColumns:
    """The three display columns that carry one send slot's numbers."""

    sent: str
    replies: str
    rate: str


#: **追記専用の唯一の情報源。** 並べ替え・削除・途中挿入はしないこと。
#: グラフは列位置 (:func:`column_index`) を固定で参照しているので、途中に1列
#: 挿すだけで全グラフが隣の列を描き始める -- しかもエラーは出ない。
#: 新しい列は役割にかかわらず末尾に足す。役割は :class:`ColumnRole` が持つので、
#: 表示列と入力列が並びとして連続している必要は無い。
ALL_COLUMNS: Final[tuple[Column, ...]] = (
    Column("cohort", "コホート", ColumnRole.DISPLAY),
    Column("sent", "送信数", ColumnRole.DISPLAY),
    Column("replies", "返信数", ColumnRole.DISPLAY),
    Column("reply_rate_pct", "返信率(%)", ColumnRole.DISPLAY),
    Column("free_sent", "無料枠_送信数", ColumnRole.DISPLAY),
    Column("free_replies", "無料枠_返信数", ColumnRole.DISPLAY),
    Column("free_rate_pct", "無料枠_返信率(%)", ColumnRole.DISPLAY),
    Column("paid_sent", "有料枠_送信数", ColumnRole.DISPLAY),
    Column("paid_replies", "有料枠_返信数", ColumnRole.DISPLAY),
    Column("paid_rate_pct", "有料枠_返信率(%)", ColumnRole.DISPLAY),
    # 9.4: 「不明」は一級市民。列ごと消すと恒等式が成り立たなくなるだけでなく、
    # 「送信枠を記録できていない」という事実そのものが見えなくなる。
    Column("unknown_sent", "不明枠_送信数", ColumnRole.DISPLAY),
    Column("unknown_replies", "不明枠_返信数", ColumnRole.DISPLAY),
    Column("unknown_rate_pct", "不明枠_返信率(%)", ColumnRole.DISPLAY),
    # 11.3: 直近コホートは返信が届き切っていないため低く出る旨の注記。
    Column("note", "注記", ColumnRole.DISPLAY),
    # ここから下は人間専用。自動化は空文字しか書かない (11.1)。
    # 誤検知を人間が否認する場合も、判定列 (表示列) を書き換えるのではなく
    # この列に書く。表示列は次の実行で必ず上書きされるので、そこに書いた否認は
    # 消えるか、あるいは自動判定として読み戻される -- どちらも事故である。
    Column("manual_review", "手動確認", ColumnRole.INPUT),
    Column("manual_note", "手動メモ", ColumnRole.INPUT),
)

DISPLAY_COLUMNS: Final[tuple[Column, ...]] = tuple(
    column for column in ALL_COLUMNS if column.role is ColumnRole.DISPLAY
)

INPUT_COLUMNS: Final[tuple[Column, ...]] = tuple(
    column for column in ALL_COLUMNS if column.role is ColumnRole.INPUT
)

#: 行を識別する列。入力列のファイル/シートと表示列を突き合わせる鍵。
ROW_KEY_COLUMN: Final[str] = "cohort"

#: 送信枠 → その枠の表示列。**明示的な写像**にしてあるのは、媒体が枠を増やした
#: ときに「集計だけ増えて列が無い」状態を防ぐため。網羅性はテストで表明する。
#: 新しい枠の列は :data:`ALL_COLUMNS` の末尾に足すこと (途中に挿さない)。
SLOT_COLUMN_KEYS: Final[dict[SendSlot, SlotColumns]] = {
    SendSlot.FREE: SlotColumns("free_sent", "free_replies", "free_rate_pct"),
    SendSlot.PAID: SlotColumns("paid_sent", "paid_replies", "paid_rate_pct"),
    SendSlot.UNKNOWN: SlotColumns("unknown_sent", "unknown_replies", "unknown_rate_pct"),
}


def column_order() -> tuple[str, ...]:
    """Every column key, in the fixed sheet order.

    先頭からの位置は **不変** として扱うこと。グラフの参照先がこの位置だから。
    """
    return tuple(column.key for column in ALL_COLUMNS)


def header_row() -> tuple[str, ...]:
    """The header cells, in the fixed sheet order."""
    return tuple(column.header for column in ALL_COLUMNS)


def display_keys() -> tuple[str, ...]:
    return tuple(column.key for column in DISPLAY_COLUMNS)


def input_keys() -> tuple[str, ...]:
    return tuple(column.key for column in INPUT_COLUMNS)


def column_index(key: str) -> int:
    """The fixed 0-based position of a column.

    グラフの仕様がここを通ることで、「グラフが参照している列」と「実際に書かれる
    列」が同じ情報源から出る。片方だけ直る事故が起きない。
    """
    for index, column in enumerate(ALL_COLUMNS):
        if column.key == key:
            return index
    raise ConfigError(f"未知の列キーです: {key!r}")


def header_of(key: str) -> str:
    """The human-facing header for a column key."""
    for column in ALL_COLUMNS:
        if column.key == key:
            return column.header
    raise ConfigError(f"未知の列キーです: {key!r}")


def role_of(key: str) -> ColumnRole:
    for column in ALL_COLUMNS:
        if column.key == key:
            return column.role
    raise ConfigError(f"未知の列キーです: {key!r}")


def is_input_column(key: str) -> bool:
    return role_of(key) is ColumnRole.INPUT


def blank_input_cell() -> str:
    """The only value the automation may ever put in an input column.

    定数を1つに絞ってあるのは、「今回だけ既定値を入れる」変更をレビューで
    見つけられるようにするため。11.1 の輪はここが緩んだ瞬間に閉じる。
    """
    return ""


def check_columns_disjoint(columns: Sequence[Column]) -> None:
    """The contract check itself, over any column list.

    列一覧を引数で受けるのは、**検査が効いていることをテストできるように**
    するため。実際の定義だけを見る検査は、通っているのか何も見ていないのかを
    区別できない。

    ``assert`` 文ではなく例外で書く。``python -O`` では ``assert`` が消え、
    **本番でだけ検査が無効になる**。検査を無効にできる形で書かないこと。

    キーだけでなくヘッダの重複も見る。シート上で人間が見分ける手がかりは
    ヘッダ文字列だけなので、同名ヘッダが2つあると入力先を間違える。
    """
    display = [column.key for column in columns if column.role is ColumnRole.DISPLAY]
    inputs = [column.key for column in columns if column.role is ColumnRole.INPUT]
    overlap = set(display) & set(inputs)
    if overlap:
        raise ConfigError(
            f"表示列と入力列が重複しています: {sorted(overlap)} -- "
            "11.1: 同じ列が出力と入力を兼ねると、取り消した誤検知が"
            "「手入力」として復活します。"
        )

    keys = [column.key for column in columns]
    if len(set(keys)) != len(keys):
        raise ConfigError(f"列キーが重複しています: {keys}")

    headers = [column.header for column in columns]
    if len(set(headers)) != len(headers):
        raise ConfigError(f"列ヘッダが重複しています: {headers}")

    if not display or not inputs:
        raise ConfigError("表示列・入力列はどちらも1列以上必要です (11.1)")


def assert_columns_disjoint() -> None:
    """Fail loudly if the declared display and input columns overlap (11.1)."""
    check_columns_disjoint(ALL_COLUMNS)


def assert_slot_columns_complete() -> None:
    """Every send slot must have display columns (9.4).

    枠を足したのに列を足していないと、その枠の送信が合計にだけ現れて内訳から
    消える。恒等式は集計側 (:class:`~analytics.aggregate.CohortRow`) では成立
    しているのに、シート上でだけ合わない、という最も気づきにくい形になる。
    """
    missing = [slot for slot in SendSlot if slot not in SLOT_COLUMN_KEYS]
    if missing:
        raise ConfigError(f"表示列が未定義の送信枠があります (9.4): {missing}")
    for slot, columns in SLOT_COLUMN_KEYS.items():
        for key in (columns.sent, columns.replies, columns.rate):
            if role_of(key) is not ColumnRole.DISPLAY:
                raise ConfigError(f"{slot} の列 {key!r} が表示列ではありません")


# 取り込み時に検査する。定義が壊れた状態のモジュールは import できない、が
# 一番早く気づける形。実行時コストは列数ぶんの走査だけ。
assert_columns_disjoint()
assert_slot_columns_complete()
