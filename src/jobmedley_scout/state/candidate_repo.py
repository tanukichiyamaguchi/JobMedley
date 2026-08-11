"""Candidate persistence, including observed ID aliases.

9.3 の4点セットのうち、ここが担うのは「両表記を試す」ための **実測アリアスの蓄積**。
正規化 (1点目) はモデル層のバリデータが、マイグレーション (3点目) は m0002 が担う。

``id_aliases`` に生の表記を貯めておくと、casefold や先頭ゼロ除去のように情報が
落ちて復元できないパターンでも、実際に観測した表記から逆引きできる。
"""

from __future__ import annotations

import sqlite3

from jobmedley_scout.clock import Clock, isoformat_utc
from jobmedley_scout.models.candidate import Candidate
from jobmedley_scout.models.ids import id_representations
from jobmedley_scout.models.text_norm import normalize_name


def upsert_candidate(
    connection: sqlite3.Connection, candidate: Candidate, *, source: str, clock: Clock
) -> None:
    now = isoformat_utc(clock.now())
    connection.execute(
        "INSERT INTO candidates ("
        "  candidate_id, raw_id_observed, display_name, name_norm, ingested_at, source"
        ") VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(candidate_id) DO UPDATE SET "
        "  display_name = excluded.display_name, "
        "  name_norm = excluded.name_norm, "
        "  raw_id_observed = excluded.raw_id_observed",
        (
            candidate.candidate_id,
            candidate.raw_id_observed,
            candidate.display_name,
            normalize_name(candidate.display_name),
            now,
            source,
        ),
    )
    # 観測した生表記を記録する。正規化後と同じでも入れておく (後で
    # 「この表記は実際に見た」と言えることに価値がある)。
    record_alias(connection, candidate.candidate_id, candidate.raw_id_observed, "observed", clock)


def record_alias(
    connection: sqlite3.Connection,
    candidate_id: str,
    alias: str,
    pattern: str,
    clock: Clock,
) -> None:
    connection.execute(
        "INSERT INTO id_aliases (alias, candidate_id, pattern, first_seen_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(alias) DO NOTHING",
        (alias, candidate_id, pattern, isoformat_utc(clock.now())),
    )


def resolve_alias(connection: sqlite3.Connection, observed: str) -> str | None:
    """Map a representation seen on the platform back to the canonical ID.

    9.3 の4点目。DBは正準形でも画面は別表記なので、照合側は両表記を試す。
    """
    row = connection.execute(
        "SELECT candidate_id FROM id_aliases WHERE alias = ?", (observed,)
    ).fetchone()
    if row is not None:
        return str(row["candidate_id"])
    # 可逆なパターンから復元できる表記も試す。
    for representation in id_representations(observed):
        row = connection.execute(
            "SELECT candidate_id FROM candidates WHERE candidate_id = ?", (representation,)
        ).fetchone()
        if row is not None:
            return str(row["candidate_id"])
    return None


def candidate_ids(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute("SELECT candidate_id FROM candidates ORDER BY ingested_at").fetchall()
    return tuple(str(row["candidate_id"]) for row in rows)


def display_name_of(connection: sqlite3.Connection, candidate_id: str) -> str | None:
    row = connection.execute(
        "SELECT display_name FROM candidates WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    return None if row is None else str(row["display_name"])


def delete_candidate(connection: sqlite3.Connection, candidate_id: str) -> None:
    """Remove a candidate and everything derived from them.

    13.2: **候補者単位の削除コマンド** (DB・エクスポート・シート行の一括削除) の
    DB側。外部キーの ON DELETE CASCADE が id_aliases と rotation_cursors を掃除する。
    送信記録は監査証跡として残す設計もありうるが、削除要求に応えるなら消すべきなので
    ここでは消す。件名も消えるため、以後その対象の返信は検知できなくなる (13.3) --
    削除要求に応じた結果なので、それが正しい。
    """
    connection.execute("DELETE FROM reply_detections WHERE candidate_id = ?", (candidate_id,))
    connection.execute("DELETE FROM followups WHERE candidate_id = ?", (candidate_id,))
    connection.execute("DELETE FROM send_records WHERE candidate_id = ?", (candidate_id,))
    connection.execute("DELETE FROM dry_run_log WHERE candidate_id = ?", (candidate_id,))
    connection.execute("DELETE FROM recon_dumps WHERE candidate_id = ?", (candidate_id,))
    connection.execute("DELETE FROM opt_outs WHERE candidate_id = ?", (candidate_id,))
    connection.execute("DELETE FROM candidates WHERE candidate_id = ?", (candidate_id,))
