"""Rotation cursor persistence.

9.6 の事故:

> 返信チェックの対象抽出が「送信の古い順に上限件数」の固定順だったため、対象が
> 上限を超えた時点から **毎回同じ最古の対象だけを再訪** し、返信が最も来やすい
> 直近の送信者を永久に見逃していた。

SQLite の ``ORDER BY last_processed_at ASC`` は NULL を先頭に置くので、
「未チェックのもの → 最も昔にチェックしたもの」という要求順序が索引そのままで
得られる。カーソルは **対象外やエラーだった場合も前進させる** -- さもないと
同じ対象で詰まる。

純粋な並べ替え・選択ロジックは :mod:`state.rotation` にあり、本モジュールは
その永続化だけを担う。
"""

from __future__ import annotations

import sqlite3

from jobmedley_scout.clock import Clock, isoformat_utc

#: ローテーションのスコープ。用途ごとに独立したカーソルを持つ。
SCOPE_REPLY_SCAN = "reply_scan"
SCOPE_RESUME_RECHECK = "resume_recheck"
SCOPE_SEND = "send"


def ensure_tracked(
    connection: sqlite3.Connection, scope: str, candidate_ids: tuple[str, ...]
) -> None:
    """Register candidates in a rotation scope without disturbing existing cursors.

    新規は ``last_processed_at IS NULL`` (未チェック) として入るので、次回の
    バッチで **最優先** に選ばれる。
    """
    connection.executemany(
        "INSERT INTO rotation_cursors (scope, candidate_id, last_processed_at, last_result) "
        "VALUES (?, ?, NULL, NULL) ON CONFLICT(scope, candidate_id) DO NOTHING",
        [(scope, candidate_id) for candidate_id in candidate_ids],
    )


def next_batch(connection: sqlite3.Connection, scope: str, limit: int) -> tuple[str, ...]:
    """The next ``limit`` candidates to process, in rotation order.

    NULL 先頭 (未チェック) → 最も昔にチェックしたもの、の順。
    """
    rows = connection.execute(
        "SELECT candidate_id FROM rotation_cursors WHERE scope = ? "
        "ORDER BY last_processed_at IS NOT NULL, last_processed_at ASC, candidate_id ASC "
        "LIMIT ?",
        (scope, limit),
    ).fetchall()
    return tuple(str(row["candidate_id"]) for row in rows)


def total_tracked(connection: sqlite3.Connection, scope: str) -> int:
    """How many candidates are in this scope.

    12.8/9.6: 「対象N件・処理M件」を必ずログに出すため。この種のバグはエラーを
    出さないので、件数を出さないと発見できない。
    """
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM rotation_cursors WHERE scope = ?", (scope,)
    ).fetchone()
    return int(row["n"])


def advance(
    connection: sqlite3.Connection, scope: str, candidate_id: str, result: str, clock: Clock
) -> None:
    """Move the cursor forward.

    **対象外やエラーだった場合も呼ぶこと。** 成功時だけ前進させると、失敗する
    対象がバッチの先頭に居座り続けて後続が永久に処理されない。
    """
    connection.execute(
        "UPDATE rotation_cursors SET last_processed_at = ?, last_result = ? "
        "WHERE scope = ? AND candidate_id = ?",
        (isoformat_utc(clock.now()), result, scope, candidate_id),
    )


def advance_all(
    connection: sqlite3.Connection,
    scope: str,
    candidate_ids: tuple[str, ...],
    result: str,
    clock: Clock,
) -> None:
    now = isoformat_utc(clock.now())
    connection.executemany(
        "UPDATE rotation_cursors SET last_processed_at = ?, last_result = ? "
        "WHERE scope = ? AND candidate_id = ?",
        [(now, result, scope, candidate_id) for candidate_id in candidate_ids],
    )


def never_processed_count(connection: sqlite3.Connection, scope: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM rotation_cursors WHERE scope = ? AND last_processed_at IS NULL",
        (scope,),
    ).fetchone()
    return int(row["n"])
