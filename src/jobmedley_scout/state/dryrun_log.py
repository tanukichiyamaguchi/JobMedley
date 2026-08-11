"""Dry-run bookkeeping.

9.1/9.2: **dry_run時は状態を一切動かさない。**

その保証を「気をつける」ではなく構造で得るために、dry run は専用テーブルにしか
書かない。``send_records`` へ到達する経路が存在しないので、重複判定のクエリで
``WHERE dry_run = 0`` を書き忘れても実害が出ないし、``ux_send_once``
(二重送信の最終防御) が dry run の行で汚れることもない。

フラグ列で区別する設計を採らなかったのはこのため。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobmedley_scout.clock import Clock, isoformat_utc
from jobmedley_scout.models.send_record import SendSlot


@dataclass(frozen=True)
class DryRunEntry:
    """What a real run *would* have sent."""

    candidate_id: str
    slot: SendSlot
    endpoint_id: str
    subject: str
    body_digest: str


def record_would_send(
    connection: sqlite3.Connection, run_id: str, entry: DryRunEntry, clock: Clock
) -> None:
    connection.execute(
        "INSERT INTO dry_run_log ("
        "  run_id, candidate_id, would_slot, would_endpoint, subject, body_digest, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            entry.candidate_id,
            str(entry.slot),
            entry.endpoint_id,
            entry.subject,
            entry.body_digest,
            isoformat_utc(clock.now()),
        ),
    )


def entries_for_run(connection: sqlite3.Connection, run_id: str) -> tuple[DryRunEntry, ...]:
    rows = connection.execute(
        "SELECT candidate_id, would_slot, would_endpoint, subject, body_digest "
        "FROM dry_run_log WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    return tuple(
        DryRunEntry(
            candidate_id=str(row["candidate_id"]),
            slot=SendSlot(row["would_slot"]),
            endpoint_id=str(row["would_endpoint"]),
            subject=str(row["subject"]),
            body_digest=str(row["body_digest"]),
        )
        for row in rows
    )


def count_for_run(connection: sqlite3.Connection, run_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM dry_run_log WHERE run_id = ?", (run_id,)
    ).fetchone()
    return int(row["n"])
