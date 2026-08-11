"""Database connection and migration runner.

12.1 の事故が本モジュールの設計理由:

> 参照実装では、送信完了後に実行が中断され (1回は失敗、3回はタイムアウトによる
> キャンセル)、状態の保存だけが走らず **送信記録56件が巻き戻った。**

対処として「状態の復元と保存を分離し、保存を送信ステップの直後にも置く」。
本モジュールは **自動コミットを使わず、呼び出し側が明示的にコミットする** 設計に
してある。``reserve_send`` が送信APIを叩く前にコミットできることが 9.2 の前提だから。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from jobmedley_scout.clock import Clock, isoformat_utc
from jobmedley_scout.errors import StateIntegrityError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

#: 整合性ウォーターマークのキー。12.1 の巻き戻り検知に使う。
META_MAX_SEND_ID = "max_send_record_id"
META_SENT_COUNT = "sent_count"


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the state database with the pragmas this system depends on."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # WAL: 送信直後のコミットが読み取りをブロックしないようにする。
    connection.execute("PRAGMA journal_mode=WAL")
    # 外部キーは既定でオフ。オンにしないと候補者削除で孤児が残る (13.2 の削除コマンド)。
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _applied_versions(connection: sqlite3.Connection) -> set[int]:
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if table is None:
        return set()
    rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row["version"]) for row in rows}


def _sql_migrations() -> list[tuple[int, Path]]:
    migrations: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("m*.sql")):
        version = int(path.name[1:5])
        migrations.append((version, path))
    return migrations


def migrate(connection: sqlite3.Connection, clock: Clock) -> tuple[int, ...]:
    """Apply pending migrations. Returns the versions applied this call.

    冪等。既に適用済みのものは飛ばす。Python 側のマイグレーション (m0002 の
    ID正規化など) は :mod:`state.migrations` の関数として別途呼ばれ、
    自己修復的に何度走っても同じ結果になるよう作ってある (9.3 の3点目)。
    """
    applied = _applied_versions(connection)
    newly_applied: list[int] = []
    for version, path in _sql_migrations():
        if version in applied:
            continue
        # ``executescript`` は実行前に暗黙のコミットを発行するため、明示的な
        # BEGIN で囲むことができない (囲むと COMMIT 時に「トランザクションが
        # 無い」と落ちる)。代わりに **スキーマ側を冪等に書いてある** --
        # m0001 の DDL はすべて IF NOT EXISTS なので、途中で落ちても次回実行が
        # 続きから適用して自己修復する。
        connection.executescript(path.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, isoformat_utc(clock.now())),
        )
        newly_applied.append(version)
    return tuple(newly_applied)


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """An explicit transaction.

    9.5: 重要度の違う書き込みを同じ失敗運命にしない。「送信済みの記録 (絶対に守る)」
    と「分析ログ (壊れても業務は回る)」を同じ処理単位に置くと、後者の失敗が前者を
    巻き添えにする。クリティカルな方を **先に、単独で** コミットすること。
    """
    connection.execute("BEGIN")
    try:
        yield connection
    except Exception:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def set_meta(connection: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    connection.execute(
        "INSERT INTO state_meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, isoformat_utc(now)),
    )


def get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def current_watermark(connection: sqlite3.Connection) -> tuple[int, int]:
    """(max send_records.id, count of status='sent') as observed right now."""
    row = connection.execute(
        "SELECT COALESCE(MAX(id), 0) AS max_id, "
        "COALESCE(SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END), 0) AS sent_count "
        "FROM send_records"
    ).fetchone()
    return int(row["max_id"]), int(row["sent_count"])


def assert_no_regression(connection: sqlite3.Connection) -> None:
    """Refuse to run if the database went backwards since the last recorded watermark.

    12.1 の巻き戻り検知。実行基盤のキャッシュから古いDBが復元されると、送信済みの
    記録が「未送信」に見えて **再送信のリスク** が生じる。しかも消えた対象は
    返信検知の対象からも外れ、件名が復元不能なので返信は恒久的に検知できなくなる。

    黙って続行するのが最悪なので、ここで止める。
    """
    stored_max = get_meta(connection, META_MAX_SEND_ID)
    stored_sent = get_meta(connection, META_SENT_COUNT)
    if stored_max is None or stored_sent is None:
        # 初回。記録が無いのは後退ではない。
        return
    observed_max, observed_sent = current_watermark(connection)
    if observed_max < int(stored_max) or observed_sent < int(stored_sent):
        raise StateIntegrityError(
            "状態データベースが後退しています "
            f"(記録: max_id={stored_max}, sent={stored_sent} / "
            f"実際: max_id={observed_max}, sent={observed_sent})。\n"
            "キャッシュから古いDBが復元された可能性があります。このまま実行すると "
            "送信済みの対象へ再送信する恐れがあるため中断しました。\n"
            "12.1: 消えた記録は件名も失われるため、その対象の返信は恒久的に検知不能になります。"
        )


def update_watermark(connection: sqlite3.Connection, now: datetime) -> None:
    """Record the current watermark. Call after every successful send step."""
    observed_max, observed_sent = current_watermark(connection)
    set_meta(connection, META_MAX_SEND_ID, str(observed_max), now)
    set_meta(connection, META_SENT_COUNT, str(observed_sent), now)


def open_state_db(db_path: Path, clock: Clock) -> sqlite3.Connection:
    """Open, migrate and integrity-check the state database."""
    connection = connect(db_path)
    migrate(connection, clock)
    assert_no_regression(connection)
    return connection
