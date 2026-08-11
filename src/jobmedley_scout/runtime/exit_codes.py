"""Exceptions to process exit codes -- the ONE place that mapping is written.

12.8: **恒久エラーは必ず異常終了させる。** 参照実装では認証切れで全APIが失敗して
いるのに、各層が警告を出して空を返したため CI は緑のまま送信0件が続いた
(:mod:`errors` の冒頭を参照)。例外を上まで通しても、最後に 0 で終わってしまえば
同じことなので、写像はここ1箇所に集約する。

キルスイッチは **異常ではなく意図的な停止** なので、恒久エラーとは別の低い番号を
持つ。運用者が実行結果を見たときに「止めた」と「壊れた」を取り違えないため。

番号の帯:

===========  ======================================================
``0``        正常
``1``-``9``  終了したが、恒久エラーの分類には当てはまらないもの
``10``-      恒久エラー。**種類ごとに固有の番号** (原因の切り分けのため)
``20``-      実行前の点検で止めたもの
===========  ======================================================
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final

from jobmedley_scout.errors import (
    ConfigError,
    KillSwitchEngaged,
    PermanentAuthError,
    StateIntegrityError,
    UnresolvedCoordinateError,
    WipeoutDetected,
)


class ExitCode(IntEnum):
    """Every exit code this program is allowed to return."""

    OK = 0

    #: 分類できなかった例外。**必ず非0** であることが要件で、番号自体は
    #: シェルの慣習 (汎用エラー) に合わせてある。
    UNKNOWN = 1
    #: 引数の誤り。argparse 自体がこの番号で終わるため合わせてある。
    USAGE = 2
    #: キルスイッチ。**エラーではない。** 意図的に止めた、という意味。
    KILL_SWITCH = 3
    #: Ctrl-C など。人間が意図的に止めた点はキルスイッチと同じ。
    INTERRUPTED = 4

    # --- 恒久エラー (12.8: 必ず異常終了) ------------------------------------
    AUTH_EXPIRED = 10
    UNRESOLVED_COORDINATE = 11
    CONFIG_INVALID = 12
    STATE_INTEGRITY = 13
    WIPEOUT = 14

    #: 起動前チェックの失敗 (12.6)。CI 側は continue-on-error を付けない。
    PREFLIGHT_FAILED = 20


#: 「止まったが壊れてはいない」終了コード。
#:
#: ``INTERRUPTED`` を **意図的に入れていない**: 送信の途中で中断された実行は、
#: 送信済みなのに状態を保存できていない可能性がある (12.1 の56件消失と同じ形)。
#: 人間が止めたことと、状態が健全であることは別の話である。
CLEAN_EXIT_CODES: Final[frozenset[ExitCode]] = frozenset({ExitCode.OK, ExitCode.KILL_SWITCH})

#: 例外型から終了コードへの表。**順序が仕様**: 上から順に ``isinstance`` で
#: 判定するので、派生クラスは基底クラスより前に置くこと。逆にすると、例えば
#: ``PermanentAuthError`` が ``PermanentError`` の行に吸われて固有の番号を失い、
#: 「認証切れなのか座標未確定なのか」が終了コードから分からなくなる。
_EXIT_CODE_TABLE: Final[tuple[tuple[type[BaseException], ExitCode], ...]] = (
    # キルスイッチを先頭に置く。恒久エラーの帯へ落ちないことを目で確認できる位置。
    (KillSwitchEngaged, ExitCode.KILL_SWITCH),
    (PermanentAuthError, ExitCode.AUTH_EXPIRED),
    (UnresolvedCoordinateError, ExitCode.UNRESOLVED_COORDINATE),
    (ConfigError, ExitCode.CONFIG_INVALID),
    (StateIntegrityError, ExitCode.STATE_INTEGRITY),
    (WipeoutDetected, ExitCode.WIPEOUT),
    (KeyboardInterrupt, ExitCode.INTERRUPTED),
)


def exit_code_for(exc: BaseException) -> int:
    """The exit code for ``exc``.

    Unknown exceptions map to :data:`ExitCode.UNKNOWN` -- non-zero on purpose.
    分からない例外を 0 で返すのは「静かなゼロ件」そのものなので、既定は必ず非0。
    """
    for exception_type, code in _EXIT_CODE_TABLE:
        if isinstance(exc, exception_type):
            return int(code)
    return int(ExitCode.UNKNOWN)


def is_clean_exit(code: int) -> bool:
    """Whether ``code`` means "nothing is broken"."""
    return code in {int(clean) for clean in CLEAN_EXIT_CODES}
