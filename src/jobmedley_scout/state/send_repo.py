"""Send-record persistence.

9.2 の要点をコードの順序として固定する:

> 「送信API成功 → DBに記録」の間にプロセスが落ちると、DB上は未送信のままになり、
> 次回二重送信する。**CIのキャンセルでも普通に起こる。**

したがって:

1. 実送信の直前に、状態を ``SENDING`` にしつつ生成した冪等キーをDBに書いて
   **コミットする** (:func:`reserve_send`)
2. そのキーを冪等キーヘッダに載せて送信する (api 層)
3. 成功したら ``SENT`` にする (:func:`mark_sent`)

``SENDING`` は「送信済み」ではないので重複判定を通らず、再試行対象として残る。
そして次回の再試行では :func:`state.idempotency.decide_key` が **同じキーを再利用**
するので、前回が実は成功していてもサーバ側の重複排除が効く。
"""

from __future__ import annotations

import sqlite3

from jobmedley_scout.clock import Clock, isoformat_utc, parse_utc
from jobmedley_scout.errors import StateIntegrityError
from jobmedley_scout.models.send_record import (
    MessageKind,
    ReservedSend,
    SendRecord,
    SendResult,
    SendSlot,
    SendStatus,
)
from jobmedley_scout.state.db import transaction, update_watermark
from jobmedley_scout.state.idempotency import KeyDecision, decide_key, new_idempotency_key


def _row_to_record(row: sqlite3.Row) -> SendRecord:
    return SendRecord(
        record_id=int(row["id"]),
        candidate_id=str(row["candidate_id"]),
        idempotency_key=str(row["idempotency_key"]),
        message_kind=MessageKind(row["message_kind"]),
        followup_seq=int(row["followup_seq"]),
        slot=SendSlot(row["send_slot"]),
        endpoint_id=str(row["endpoint_id"]),
        subject=str(row["subject"]),
        status=SendStatus(row["status"]),
        reserved_at=parse_utc(str(row["reserved_at"])),
        sent_at=parse_utc(str(row["sent_at"])) if row["sent_at"] else None,
        failure_reason=str(row["failure_reason"]) if row["failure_reason"] else None,
    )


def records_for(
    connection: sqlite3.Connection, candidate_id: str, kind: MessageKind | None = None
) -> tuple[SendRecord, ...]:
    if kind is None:
        rows = connection.execute(
            "SELECT * FROM send_records WHERE candidate_id = ? ORDER BY id", (candidate_id,)
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM send_records WHERE candidate_id = ? AND message_kind = ? ORDER BY id",
            (candidate_id, str(kind)),
        ).fetchall()
    return tuple(_row_to_record(row) for row in rows)


def latest_record(
    connection: sqlite3.Connection,
    candidate_id: str,
    kind: MessageKind,
    followup_seq: int = 0,
) -> SendRecord | None:
    row = connection.execute(
        "SELECT * FROM send_records "
        "WHERE candidate_id = ? AND message_kind = ? AND followup_seq = ? "
        "ORDER BY id DESC LIMIT 1",
        (candidate_id, str(kind), followup_seq),
    ).fetchone()
    return None if row is None else _row_to_record(row)


def sent_count(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM send_records WHERE status = 'sent'"
    ).fetchone()
    return int(row["n"])


def sent_count_by_slot(connection: sqlite3.Connection) -> dict[SendSlot, int]:
    """Counts per slot. 9.4 の恒等式「合計 == 有料 + 無料 + 不明」を保つため、
    ``UNKNOWN`` も必ずキーとして返す (0件でも省略しない)。"""
    counts: dict[SendSlot, int] = {slot: 0 for slot in SendSlot}
    rows = connection.execute(
        "SELECT send_slot, COUNT(*) AS n FROM send_records WHERE status = 'sent' "
        "GROUP BY send_slot"
    ).fetchall()
    for row in rows:
        counts[SendSlot(row["send_slot"])] = int(row["n"])
    return counts


def reserve_send(
    connection: sqlite3.Connection,
    *,
    candidate_id: str,
    message_kind: MessageKind,
    followup_seq: int,
    slot: SendSlot,
    endpoint_id: str,
    subject: str,
    subject_norm: str,
    subject_prefix35: str,
    body_digest: str,
    run_id: str,
    provenance: str,
    clock: Clock,
) -> ReservedSend:
    """Commit a ``SENDING`` row **before** the network call, and return its key.

    この関数が返った時点で、冪等キーは既にディスク上にある。以降どこでプロセスが
    落ちても、次回実行は「送ったか不明」を正しく認識できる (9.2)。

    件名は必須。復元不能で、失うとその対象の返信は恒久的に検知不能になる (13.3)。
    """
    if not subject.strip():
        raise StateIntegrityError(
            "件名が空のまま送信を予約しようとしました。件名は返信検知の唯一の突合キーで、"
            "後から復元できません (13.3)。"
        )

    prior = latest_record(connection, candidate_id, message_kind, followup_seq)
    decision = decide_key(prior)
    if decision is KeyDecision.REUSE and prior is not None:
        # 前回が SENDING (送ったか不明)。同じキーで送り直し、サーバ側の重複排除に委ねる。
        key = prior.idempotency_key
    else:
        # 確定失敗・未送信からの送信は新しいキー。正当な再試行がサーバに弾かれないため。
        key = new_idempotency_key()

    now = clock.now()
    with transaction(connection):
        if decision is KeyDecision.REUSE and prior is not None:
            connection.execute(
                "UPDATE send_records SET status = 'sending', reserved_at = ?, "
                "attempt_no = attempt_no + 1, run_id = ? WHERE id = ?",
                (isoformat_utc(now), run_id, prior.record_id),
            )
            record_id = prior.record_id
        else:
            cursor = connection.execute(
                "INSERT INTO send_records ("
                "  candidate_id, idempotency_key, message_kind, followup_seq, send_slot,"
                "  endpoint_id, subject, subject_norm, subject_prefix35, body_digest,"
                "  status, reserved_at, provenance, run_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sending', ?, ?, ?)",
                (
                    candidate_id,
                    key,
                    str(message_kind),
                    followup_seq,
                    str(slot),
                    endpoint_id,
                    subject,
                    subject_norm,
                    subject_prefix35,
                    body_digest,
                    isoformat_utc(now),
                    provenance,
                    run_id,
                ),
            )
            record_id = int(cursor.lastrowid or 0)

    return ReservedSend(
        record_id=record_id,
        candidate_id=candidate_id,
        idempotency_key=key,
        message_kind=message_kind,
        followup_seq=followup_seq,
        slot=slot,
        subject=subject,
        reserved_at=now,
    )


def mark_sent(
    connection: sqlite3.Connection, reserved: ReservedSend, result: SendResult, clock: Clock
) -> None:
    """Commit the send record. **Do this before any analytics write** (9.5).

    9.4: 送信枠とエンドポイントは実測値として保存する。後から endpoint_id 経由で
    導出すると、写像が変わったときに過去行が黙って書き換わる。
    """
    now = clock.now()
    with transaction(connection):
        connection.execute(
            "UPDATE send_records SET status = 'sent', sent_at = ?, http_status = ?, "
            "platform_message_id = ?, send_slot = ?, endpoint_id = ? WHERE id = ?",
            (
                isoformat_utc(now),
                result.http_status,
                result.platform_message_id,
                str(result.slot),
                result.endpoint_id,
                reserved.record_id,
            ),
        )
        update_watermark(connection, now)


def mark_failed(
    connection: sqlite3.Connection,
    reserved: ReservedSend,
    reason: str,
    clock: Clock,
    http_status: int | None = None,
) -> None:
    """Record a *confirmed* failure.

    確定失敗なので、次回は新しい冪等キーが発行される (9.2)。「送ったか不明」の
    ``SENDING`` のまま残してはいけない -- そちらはキー再利用の対象になる。
    """
    now = clock.now()
    with transaction(connection):
        connection.execute(
            "UPDATE send_records SET status = 'failed', failed_at = ?, failure_reason = ?, "
            "http_status = ? WHERE id = ?",
            (isoformat_utc(now), reason, http_status, reserved.record_id),
        )
        update_watermark(connection, now)


def mark_skipped(
    connection: sqlite3.Connection, reserved: ReservedSend, reason: str, clock: Clock
) -> None:
    with transaction(connection):
        connection.execute(
            "UPDATE send_records SET status = 'skipped', failure_reason = ? WHERE id = ?",
            (reason, reserved.record_id),
        )
        update_watermark(connection, clock.now())


def stuck_sending(connection: sqlite3.Connection) -> tuple[SendRecord, ...]:
    """Rows left in ``SENDING`` -- i.e. sends whose outcome is unknown.

    運用監視用。件数が増え続けるなら、送信直後に落ちる何かが起きている。
    """
    rows = connection.execute(
        "SELECT * FROM send_records WHERE status = 'sending' ORDER BY reserved_at"
    ).fetchall()
    return tuple(_row_to_record(row) for row in rows)


def subject_index_rows(connection: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
    """Everything the reply matcher needs to build its subject index (10.2)."""
    return tuple(
        connection.execute(
            "SELECT id, candidate_id, subject_norm, subject_prefix35 FROM send_records "
            "WHERE status = 'sent' ORDER BY id"
        ).fetchall()
    )
