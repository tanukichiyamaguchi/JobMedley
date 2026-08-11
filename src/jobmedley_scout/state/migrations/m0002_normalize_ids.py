"""Startup migration: fold existing rows onto their canonical candidate IDs.

9.3 の4点セットの **3点目**。モデル層のバリdatorは今後の取り込みを守るが、
既にDBに入ってしまった行は直せない。このマイグレーションがそれを直す。

性質:

* **冪等** -- 正規化済みのIDを正規化しても恒等。何度走っても同じ結果。
* **自己修復** -- 途中で落ちても、次回の実行が続きから直す。
* 毎回起動時に走らせてよい (走らせること)。

衝突時の方針 (**コード内に明記するのが仕様**):

1. 送信済み (``status='sent'``) の記録を持つ側を残す
2. 同条件なら ``ingested_at`` が古い方を残す
3. ただし片側に返信検知があれば、**返信あり側を優先** する

返信を優先するのは、返信は取り返しがつかない事実であり、失うと分析が狂うため。
全マージは ``id_migration_log`` に残す。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jobmedley_scout.clock import Clock, isoformat_utc
from jobmedley_scout.models.ids import active_id_patterns, normalize_candidate_id


@dataclass(frozen=True)
class MigrationReport:
    scanned: int
    rewritten: int
    merged: int

    def render(self) -> str:
        return (
            f"ID正規化マイグレーション: 走査 {self.scanned} 件 / "
            f"書き換え {self.rewritten} 件 / 統合 {self.merged} 件"
        )


def _has_sent(connection: sqlite3.Connection, candidate_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM send_records WHERE candidate_id = ? AND status = 'sent' LIMIT 1",
        (candidate_id,),
    ).fetchone()
    return row is not None


def _has_reply(connection: sqlite3.Connection, candidate_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM reply_detections WHERE candidate_id = ? LIMIT 1", (candidate_id,)
    ).fetchone()
    return row is not None


def _ingested_at(connection: sqlite3.Connection, candidate_id: str) -> str:
    row = connection.execute(
        "SELECT ingested_at FROM candidates WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    return "" if row is None else str(row["ingested_at"])


def _choose_survivor(connection: sqlite3.Connection, left: str, right: str) -> tuple[str, str]:
    """Pick which of two colliding IDs survives. Returns (survivor, reason)."""
    # 3. 返信ありを最優先 (取り返しがつかない事実だから)。
    left_reply, right_reply = _has_reply(connection, left), _has_reply(connection, right)
    if left_reply != right_reply:
        survivor = left if left_reply else right
        return survivor, "返信検知あり側を優先"
    # 1. 送信済みを優先。
    left_sent, right_sent = _has_sent(connection, left), _has_sent(connection, right)
    if left_sent != right_sent:
        survivor = left if left_sent else right
        return survivor, "送信済み側を優先"
    # 2. 古い方を残す。
    if _ingested_at(connection, left) <= _ingested_at(connection, right):
        return left, "取り込みが古い側を優先"
    return right, "取り込みが古い側を優先"


def _log_merge(
    connection: sqlite3.Connection,
    from_id: str,
    to_id: str,
    pattern: str,
    resolution: str | None,
    clock: Clock,
) -> None:
    connection.execute(
        "INSERT INTO id_migration_log (applied_at, from_id, to_id, pattern, collision_resolution) "
        "VALUES (?, ?, ?, ?, ?)",
        (isoformat_utc(clock.now()), from_id, to_id, pattern, resolution),
    )


def _repoint(connection: sqlite3.Connection, from_id: str, to_id: str) -> None:
    """Move every dependent row from ``from_id`` to ``to_id``."""
    # 送信記録は ux_send_once (部分ユニーク) に当たりうる。当たった行は
    # 「同じ相手に同じ種別で送った重複記録」なので、片方を skipped に落として残す
    # -- 消すと送信した事実が失われる (13.3)。
    duplicates = connection.execute(
        "SELECT s.id FROM send_records s WHERE s.candidate_id = ? AND s.status = 'sent' "
        "AND EXISTS (SELECT 1 FROM send_records t WHERE t.candidate_id = ? "
        "  AND t.status = 'sent' AND t.message_kind = s.message_kind "
        "  AND t.followup_seq = s.followup_seq)",
        (from_id, to_id),
    ).fetchall()
    for row in duplicates:
        connection.execute(
            "UPDATE send_records SET status = 'skipped', "
            "failure_reason = 'ID正規化により統合された重複記録' WHERE id = ?",
            (int(row["id"]),),
        )

    connection.execute(
        "UPDATE send_records SET candidate_id = ? WHERE candidate_id = ?", (to_id, from_id)
    )
    connection.execute(
        "UPDATE OR IGNORE reply_detections SET candidate_id = ? WHERE candidate_id = ?",
        (to_id, from_id),
    )
    connection.execute("DELETE FROM reply_detections WHERE candidate_id = ?", (from_id,))
    connection.execute(
        "UPDATE OR IGNORE followups SET candidate_id = ? WHERE candidate_id = ?", (to_id, from_id)
    )
    connection.execute("DELETE FROM followups WHERE candidate_id = ?", (from_id,))
    connection.execute(
        "UPDATE OR IGNORE rotation_cursors SET candidate_id = ? WHERE candidate_id = ?",
        (to_id, from_id),
    )
    connection.execute("DELETE FROM rotation_cursors WHERE candidate_id = ?", (from_id,))
    connection.execute(
        "UPDATE OR IGNORE id_aliases SET candidate_id = ? WHERE candidate_id = ?", (to_id, from_id)
    )
    connection.execute("DELETE FROM id_aliases WHERE candidate_id = ?", (from_id,))
    connection.execute(
        "UPDATE OR IGNORE opt_outs SET candidate_id = ? WHERE candidate_id = ?", (to_id, from_id)
    )
    connection.execute("DELETE FROM opt_outs WHERE candidate_id = ?", (from_id,))
    connection.execute("DELETE FROM candidates WHERE candidate_id = ?", (from_id,))


def run(connection: sqlite3.Connection, clock: Clock) -> MigrationReport:
    """Normalize every stored candidate ID. Safe to run on every startup."""
    pattern_names = ",".join(p.name for p in active_id_patterns()) or "base-only"
    rows = connection.execute("SELECT candidate_id FROM candidates").fetchall()
    scanned = len(rows)
    rewritten = 0
    merged = 0

    for row in rows:
        stored = str(row["candidate_id"])
        try:
            canonical = normalize_candidate_id(stored)
        except ValueError:
            # 正規化できないIDは触らない。壊れたデータを勝手に作り変えるより、
            # 残して人間が気づけるようにするほうが安全。
            continue
        if canonical == stored:
            continue

        # 保存済みの生表記としてアリアスに残す (両表記照合のため)。
        connection.execute(
            "INSERT INTO id_aliases (alias, candidate_id, pattern, first_seen_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(alias) DO NOTHING",
            (stored, canonical, pattern_names, isoformat_utc(clock.now())),
        )

        exists = connection.execute(
            "SELECT 1 FROM candidates WHERE candidate_id = ?", (canonical,)
        ).fetchone()
        if exists is None:
            connection.execute(
                "UPDATE candidates SET candidate_id = ? WHERE candidate_id = ?",
                (canonical, stored),
            )
            connection.execute(
                "UPDATE send_records SET candidate_id = ? WHERE candidate_id = ?",
                (canonical, stored),
            )
            connection.execute(
                "UPDATE reply_detections SET candidate_id = ? WHERE candidate_id = ?",
                (canonical, stored),
            )
            connection.execute(
                "UPDATE followups SET candidate_id = ? WHERE candidate_id = ?", (canonical, stored)
            )
            connection.execute(
                "UPDATE rotation_cursors SET candidate_id = ? WHERE candidate_id = ?",
                (canonical, stored),
            )
            connection.execute(
                "UPDATE opt_outs SET candidate_id = ? WHERE candidate_id = ?", (canonical, stored)
            )
            connection.execute(
                "UPDATE id_aliases SET candidate_id = ? WHERE candidate_id = ?",
                (canonical, stored),
            )
            _log_merge(connection, stored, canonical, pattern_names, None, clock)
            rewritten += 1
        else:
            survivor, reason = _choose_survivor(connection, stored, canonical)
            loser = canonical if survivor == stored else stored
            if survivor == stored:
                # 正準形側を捨てて生表記側を残すことはしない -- IDは正準形で
                # 揃える必要がある。生表記側のデータを正準形へ寄せる。
                _repoint(connection, loser, stored)
                connection.execute(
                    "UPDATE candidates SET candidate_id = ? WHERE candidate_id = ?",
                    (canonical, stored),
                )
                connection.execute(
                    "UPDATE send_records SET candidate_id = ? WHERE candidate_id = ?",
                    (canonical, stored),
                )
                connection.execute(
                    "UPDATE reply_detections SET candidate_id = ? WHERE candidate_id = ?",
                    (canonical, stored),
                )
                connection.execute(
                    "UPDATE followups SET candidate_id = ? WHERE candidate_id = ?",
                    (canonical, stored),
                )
                connection.execute(
                    "UPDATE rotation_cursors SET candidate_id = ? WHERE candidate_id = ?",
                    (canonical, stored),
                )
                connection.execute(
                    "UPDATE opt_outs SET candidate_id = ? WHERE candidate_id = ?",
                    (canonical, stored),
                )
            else:
                _repoint(connection, stored, canonical)
            _log_merge(connection, stored, canonical, pattern_names, reason, clock)
            merged += 1

    return MigrationReport(scanned=scanned, rewritten=rewritten, merged=merged)
