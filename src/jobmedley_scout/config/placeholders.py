"""Site coordinates that have not yet been confirmed against real data.

指示書の一貫した方針は「推測で埋めないこと」(原則3・6.4・8.3・16章)。
本モジュールはそれを **型レベルで強制** するための機構である。

未確定の値を ``None`` で表さないのは意図的である。``None`` は「確認した結果
存在しなかった」という正当な値であり (例: この媒体に2段階認証は無い)、それと
「まだ確認していない」を混同することは、指示書が禁じている推測そのものになる。

保護は発火順に4層:

1. **型検査時** -- ``Coord[T]`` は共用体なので、mypy strict は ``Coord[str]`` を
   ``str`` の引数へ渡すことを拒否する。``require()`` が唯一の合法な絞り込み。
2. **設定読込時** -- 全キーが存在必須・``extra="forbid"``・安全critical項目は
   既定値なし。打鍵ミスも省略も検証エラーになる (7.6)。
3. **コマンド開始時** -- ``config.audit.assert_ready_for()`` がコマンド別の
   必須座標集合を検査する。
4. **実行時最終防壁** -- 番兵の ``__bool__`` / ``__str__`` / ``__format__`` が
   例外を送出する。``Any`` 経由の抜け穴や動的参照はここで止まる。
"""

from __future__ import annotations

from enum import StrEnum
from typing import NoReturn, TypeAlias, TypeVar

from jobmedley_scout.errors import UnresolvedCoordinateError

# config/site_coordinates.yaml で未確定を表す literal。
# 空文字や null ではなく明示的な語にしているのは、「書き忘れ」と
# 「まだ確認していないと明示した」を読み手が区別できるようにするため。
UNRESOLVED_TOKEN = "UNRESOLVED"


class LadderStage(StrEnum):
    """3章の実装ラダー。座標はどの段で埋まるかが決まっている。"""

    STAGE_1_LOGIN = "段階1: ログインセッションの取得"
    STAGE_2_PREFLIGHT = "段階2: 起動前チェック"
    STAGE_3_RECON = "段階3: 偵察 (内部APIの特定)"
    STAGE_4_DRYRUN_API = "段階4: dryRun付きの送信検証"
    STAGE_5_DRYRUN_FULL = "段階5: dry_run=true での全体実行"
    STAGE_6_LIVE_SMALL = "段階6: 少件数での本番送信"
    STAGE_7_SCALE_UP = "段階7: 上限の段階的な引き上げ"


class Unresolved:
    """A coordinate that has not been confirmed. Using it raises.

    ``__repr__`` は **意図的に例外を出さない**。デバッガ・pytest のアサーション
    表示・ログの ``%r`` が壊れると、原因の特定そのものができなくなるため。
    ``__eq__`` / ``__hash__`` も既定 (同一性ベース) のままにしてあるので、
    dict のキーや set の要素として安全に扱える。
    """

    __slots__ = ("key", "stage", "how_to_obtain")

    def __init__(self, key: str, stage: LadderStage, how_to_obtain: str) -> None:
        self.key = key
        self.stage = stage
        self.how_to_obtain = how_to_obtain

    def _raise(self, operation: str) -> NoReturn:
        raise UnresolvedCoordinateError(self, used_by=f"<{operation}>")

    # 真偽値評価を殺す。`if cfg.send_url:` のような何気ない分岐が、
    # 未確定座標を「偽」として黙って読み飛ばすのを防ぐ。
    def __bool__(self) -> NoReturn:
        self._raise("bool()")

    # 文字列化を殺す。f-string 補間・str.format・"".join がここを通る。
    # 未確定のURLがそのまま組み立てられて送信されるのを防ぐ最後の砦。
    def __str__(self) -> NoReturn:
        self._raise("str()")

    def __format__(self, _spec: str) -> NoReturn:
        self._raise("format()")

    def __repr__(self) -> str:
        # 例外を出さない。理由は class docstring を参照。
        return f"<Unresolved {self.key!r} stage={self.stage.name}>"


T = TypeVar("T")

#: A site coordinate: either a confirmed value of type ``T``, or ``Unresolved``.
#:
#: mypy strict の下では ``Coord[str]`` を ``str`` に代入できない。これが
#: 「推測で埋めない」を人間の注意力ではなく型検査に守らせる仕組み。
Coord: TypeAlias = T | Unresolved


def is_resolved(coordinate: Coord[T]) -> bool:
    """Whether a coordinate has been confirmed.

    値そのものを取り出すには :func:`require` を使うこと。この関数は
    「未確定なら別の経路を採る」ような分岐のためにある。
    """
    return not isinstance(coordinate, Unresolved)


def require(coordinate: Coord[T], *, used_by: str) -> T:
    """Narrow a coordinate to its value, or raise naming the caller.

    ``used_by`` は呼び出し元を人間に分かる形で書くこと (例:
    ``"api.send.send_message"``)。例外メッセージが「どこで詰まったか」と
    「どうやって埋めるか」の両方を含むようになり、traceback がそのまま
    手順書になる。
    """
    if isinstance(coordinate, Unresolved):
        raise UnresolvedCoordinateError(coordinate, used_by=used_by)
    return coordinate


def resolved_or(coordinate: Coord[T], fallback: T) -> T:
    """Return the coordinate's value, or ``fallback`` when unconfirmed.

    **本当に任意の座標にのみ使うこと。** 送信先URL・成功ステータスのような
    「無いと業務が成立しない」座標にこれを使うと、未確定のまま既定値で
    走り出すという、7.6 で塞いだはずの事故がここから再発する。
    """
    if isinstance(coordinate, Unresolved):
        return fallback
    return coordinate
