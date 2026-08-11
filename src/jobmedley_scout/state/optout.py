"""Opt-out and suppression list.

9.9 の警告:

> 参照実装は媒体ネイティブの追客に委譲しているため表面化していませんが、
> **自前再送の経路は返信済みかどうかを見ていません。** ネイティブ追客がない媒体へ
> 移植すると即座に事故になります。

ジョブメドレーに媒体標準の追客があるかは未確定 (座標 ``followup.native_supported``)
なので、**自前で抑止リストを持つ**。媒体側のチェックに依存しない。

順序も仕様である: **返信同期を追客実行より前に走らせる** (:mod:`runtime.pipeline`
がこの順序を固定する)。返信を取り込む前に追客を撃つと、返信済みの相手に追客が飛ぶ。
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from jobmedley_scout.clock import Clock, isoformat_utc


class OptOutReason(StrEnum):
    REPLIED = "replied"
    DECLINED = "declined"
    OPT_OUT = "opt_out"
    MANUAL = "manual"


def record_opt_out(
    connection: sqlite3.Connection,
    candidate_id: str,
    reason: OptOutReason,
    *,
    source: str,
    clock: Clock,
    permanent: bool = False,
    note: str | None = None,
) -> None:
    """Add or upgrade a suppression entry.

    ``permanent`` は候補者からの明示的な送信停止要求 (9.9)。以後の **全送信** から
    恒久的に除外する。一度 permanent になったものは、後続の弱い理由で降格しない。
    """
    connection.execute(
        "INSERT INTO opt_outs (candidate_id, reason, permanent, source, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(candidate_id) DO UPDATE SET "
        "  reason = excluded.reason, "
        "  source = excluded.source, "
        "  note = excluded.note, "
        # 恒久フラグは立つ方向にしか動かさない。降格させると停止要求が消える。
        "  permanent = MAX(opt_outs.permanent, excluded.permanent)",
        (
            candidate_id,
            str(reason),
            1 if permanent else 0,
            source,
            note,
            isoformat_utc(clock.now()),
        ),
    )


def is_suppressed(connection: sqlite3.Connection, candidate_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM opt_outs WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    return row is not None


def suppressed_ids(connection: sqlite3.Connection) -> frozenset[str]:
    """Every suppressed candidate. Consult this **immediately before** a follow-up send."""
    rows = connection.execute("SELECT candidate_id FROM opt_outs").fetchall()
    return frozenset(str(row["candidate_id"]) for row in rows)


def sync_replies_into_suppression(connection: sqlite3.Connection, clock: Clock) -> int:
    """Fold the active reply-detection run into the suppression list.

    9.9: 追客の送信直前に、返信済み・辞退・送信停止要求の抑止リストを参照する。
    返信検知は run スコープで全量置換される (10.4) ので、ここは冪等に呼べる。

    Returns the number of newly suppressed candidates.
    """
    now = isoformat_utc(clock.now())
    cursor = connection.execute(
        "INSERT INTO opt_outs (candidate_id, reason, permanent, source, note, created_at) "
        "SELECT d.candidate_id, 'replied', 0, 'reply-sync', NULL, ? "
        "FROM v_replies_active d "
        "WHERE d.candidate_id NOT IN (SELECT candidate_id FROM opt_outs)",
        (now,),
    )
    return cursor.rowcount if cursor.rowcount > 0 else 0


def clear_reply_suppression(connection: sqlite3.Connection) -> int:
    """Remove suppressions that came from reply sync only.

    10.4: 誤検知が一度DBに書かれると手作業では消せない -- ので、返信由来の抑止は
    自己修復できる必要がある。恒久的な停止要求 (permanent) と手動指定は残す。
    """
    cursor = connection.execute(
        "DELETE FROM opt_outs WHERE source = 'reply-sync' AND permanent = 0"
    )
    return cursor.rowcount if cursor.rowcount > 0 else 0
