"""End-to-end state tests against a real (temporary) SQLite database.

ここで確かめるのは 9.2 と 12.1 の **順序** である。純粋関数のテスト
(``test_idempotency.py`` 等) が判断を確かめるのに対し、こちらは
「その判断が実際にディスクへ、正しい順番で書かれるか」を確かめる。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobmedley_scout.clock import FixedClock
from jobmedley_scout.errors import StateIntegrityError
from jobmedley_scout.models.send_record import (
    MessageKind,
    SendResult,
    SendSlot,
    SendStatus,
)
from jobmedley_scout.state import send_repo
from jobmedley_scout.state.db import (
    assert_no_regression,
    connect,
    migrate,
    update_watermark,
)

START = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)


@pytest.fixture()
def clock() -> FixedClock:
    return FixedClock(START)


@pytest.fixture()
def db(tmp_path: Path, clock: FixedClock) -> sqlite3.Connection:
    connection = connect(tmp_path / "scout.db")
    migrate(connection, clock)
    connection.execute(
        "INSERT INTO candidates (candidate_id, raw_id_observed, display_name, name_norm,"
        " ingested_at, source) VALUES ('C1', 'C1', '山田太郎', 'やまだたろう', ?, 'test')",
        ("2026-08-11T00:00:00Z",),
    )
    return connection


def _reserve(db: sqlite3.Connection, clock: FixedClock, subject: str = "はじめまして、ご連絡です"):
    return send_repo.reserve_send(
        db,
        candidate_id="C1",
        message_kind=MessageKind.FIRST_CONTACT,
        followup_seq=0,
        slot=SendSlot.PAID,
        endpoint_id="send.paid",
        subject=subject,
        subject_norm=subject,
        subject_prefix35=subject[:35],
        body_digest="digest",
        run_id="run-1",
        provenance="auto/pipeline",
        clock=clock,
    )


def test_key_is_on_disk_before_the_send_call(db: sqlite3.Connection, clock: FixedClock) -> None:
    """9.2: 冪等キーは **送信APIを叩く前に** コミットされていること。

    参照実装の事故は「送信API成功 → DBに記録」の間で落ちること。CIのキャンセルでも
    普通に起こる。reserve_send が返った時点でキーがディスクにあるなら、以降どこで
    落ちても次回実行は「送ったか不明」を正しく認識できる。
    """
    reserved = _reserve(db, clock)

    # 別コネクションから読む = 本当にコミットされている。
    row = db.execute(
        "SELECT status, idempotency_key, subject FROM send_records WHERE id = ?",
        (reserved.record_id,),
    ).fetchone()
    assert row["status"] == SendStatus.SENDING.value
    assert row["idempotency_key"] == reserved.idempotency_key
    assert row["subject"] == "はじめまして、ご連絡です"


def test_empty_subject_is_refused(db: sqlite3.Connection, clock: FixedClock) -> None:
    """13.3: 件名は復元不能。失うとその対象の返信は恒久的に検知できなくなる。"""
    with pytest.raises(StateIntegrityError):
        _reserve(db, clock, subject="   ")


def test_retry_from_sending_reuses_the_same_key(db: sqlite3.Connection, clock: FixedClock) -> None:
    """9.2: 前回が SENDING (送ったか不明) なら **同じキーを再利用** する。

    前回が実は成功していても、サーバ側の重複排除が効く。
    """
    first = _reserve(db, clock)
    # 送信結果を書かずに次の実行が来た = プロセスが落ちた状況。
    clock.advance(timedelta(hours=1))
    second = _reserve(db, clock)

    assert second.idempotency_key == first.idempotency_key
    assert second.record_id == first.record_id
    # 行が増えていないこと。
    assert db.execute("SELECT COUNT(*) AS n FROM send_records").fetchone()["n"] == 1


def test_retry_after_confirmed_failure_issues_a_new_key(
    db: sqlite3.Connection, clock: FixedClock
) -> None:
    """9.2: 確定失敗からの送信は **新しいキー**。

    正当な再試行がサーバに弾かれないため。
    """
    first = _reserve(db, clock)
    send_repo.mark_failed(db, first, "HTTP 500", clock)

    clock.advance(timedelta(hours=1))
    second = _reserve(db, clock)
    assert second.idempotency_key != first.idempotency_key


def test_sent_status_blocks_a_second_send_at_the_database_level(
    db: sqlite3.Connection, clock: FixedClock
) -> None:
    """``ux_send_once`` はコードのバグを生き延びる唯一の二重送信防御。

    部分ユニークインデックスなので、status='sent' の行だけが制約対象になる。
    """
    reserved = _reserve(db, clock)
    send_repo.mark_sent(
        db,
        reserved,
        SendResult(
            candidate_id="C1",
            endpoint_id="send.paid",
            slot=SendSlot.PAID,
            succeeded=True,
            http_status=201,
            platform_message_id="m1",
            failure_reason=None,
            idempotency_key=reserved.idempotency_key,
        ),
        clock,
    )

    # 同じ相手・同じ種別で2件目の 'sent' 行を直接ねじ込もうとしても DB が拒否する。
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO send_records (candidate_id, idempotency_key, message_kind,"
            " followup_seq, send_slot, endpoint_id, subject, subject_norm,"
            " subject_prefix35, body_digest, status, reserved_at, provenance, run_id)"
            " VALUES ('C1', 'other-key', 'first_contact', 0, 'paid', 'send.paid',"
            " 's', 's', 's', 'd', 'sent', ?, 'p', 'r')",
            ("2026-08-11T04:00:00Z",),
        )


def test_slot_counts_keep_the_identity(db: sqlite3.Connection, clock: FixedClock) -> None:
    """9.4: 「合計 == 有料 + 無料 + 不明」。UNKNOWN は0件でもキーとして残る。"""
    reserved = _reserve(db, clock)
    send_repo.mark_sent(
        db,
        reserved,
        SendResult(
            candidate_id="C1",
            endpoint_id="send.paid",
            slot=SendSlot.PAID,
            succeeded=True,
            http_status=201,
            platform_message_id=None,
            failure_reason=None,
            idempotency_key=reserved.idempotency_key,
        ),
        clock,
    )
    counts = send_repo.sent_count_by_slot(db)
    assert set(counts) == set(SendSlot)
    assert sum(counts.values()) == send_repo.sent_count(db)
    assert counts[SendSlot.UNKNOWN] == 0


def test_database_regression_is_detected(
    db: sqlite3.Connection, clock: FixedClock, tmp_path: Path
) -> None:
    """12.1: 古いDBがキャッシュから復元されたら **止める**。

    参照実装ではこれを検知できず、消えた記録が「未送信」に見えて再送信のリスクが
    生じた。しかも件名も失われるため、その対象の返信は恒久的に検知不能になった。
    """
    reserved = _reserve(db, clock)
    send_repo.mark_sent(
        db,
        reserved,
        SendResult(
            candidate_id="C1",
            endpoint_id="send.paid",
            slot=SendSlot.PAID,
            succeeded=True,
            http_status=201,
            platform_message_id=None,
            failure_reason=None,
            idempotency_key=reserved.idempotency_key,
        ),
        clock,
    )
    # ここまでのウォーターマークは記録済み (mark_sent が更新している)。
    assert_no_regression(db)  # 問題なし

    # 記録が消えた状況を作る = 古いDBの復元と同じ。
    db.execute("DELETE FROM send_records")
    with pytest.raises(StateIntegrityError) as excinfo:
        assert_no_regression(db)
    assert "後退" in str(excinfo.value)


def test_watermark_starts_clean_on_a_fresh_database(tmp_path: Path, clock: FixedClock) -> None:
    """初回実行は「記録が無い」だけであって後退ではない。"""
    connection = connect(tmp_path / "fresh.db")
    migrate(connection, clock)
    assert_no_regression(connection)  # 例外にならないこと

    update_watermark(connection, clock.now())
    assert_no_regression(connection)


def test_subject_index_rows_only_include_sent(db: sqlite3.Connection, clock: FixedClock) -> None:
    """10.2: 返信の突合に使うのは実際に送った件名だけ。"""
    reserved = _reserve(db, clock)
    assert send_repo.subject_index_rows(db) == ()  # まだ sending

    send_repo.mark_sent(
        db,
        reserved,
        SendResult(
            candidate_id="C1",
            endpoint_id="send.paid",
            slot=SendSlot.PAID,
            succeeded=True,
            http_status=201,
            platform_message_id=None,
            failure_reason=None,
            idempotency_key=reserved.idempotency_key,
        ),
        clock,
    )
    rows = send_repo.subject_index_rows(db)
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "C1"
