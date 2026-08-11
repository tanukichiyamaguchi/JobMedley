-- 初期スキーマ。
--
-- 設計の要点は3つ。
--
-- 1. **ux_send_once** -- コードのバグを生き延びる唯一の二重送信防御。部分
--    ユニークインデックスなので、status='sent' の行だけが制約対象になり、
--    再試行可能な generated/failed/sending は何行あってもよい (9.1)。
--
-- 2. **send_records.subject が NOT NULL** -- 件名は後から復元できず、失うと
--    その対象の返信は恒久的に検知不可能になる (13.3)。返信検知の突合キーが
--    件名だから (10.2)。1行目から必須にしてある。
--
-- 3. **dry_run は別テーブル** -- dry_run_log にしか書かないので、重複判定の
--    クエリで WHERE dry_run = 0 を書き忘れても実害が出ない。dry_run時は
--    状態を一切動かさない (9.2) が構造で保証される。

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

-- 整合性ウォーターマーク。データ本体とは別テーブルにしてあるので、
-- 12.1 の巻き戻り (キャッシュ復元で送信記録が消える) を検知できる。
CREATE TABLE IF NOT EXISTS state_meta (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
  candidate_id      TEXT PRIMARY KEY,   -- 正規化済み。モデル層のバリデータが強制 (9.3)
  raw_id_observed   TEXT NOT NULL,      -- 媒体が実際に返した表記
  display_name      TEXT NOT NULL,
  name_norm         TEXT NOT NULL,      -- 自己除外と返信者名の手掛かり用
  ingested_at       TEXT NOT NULL,
  source            TEXT NOT NULL,
  resume_fetched_at TEXT,
  resume_digest     TEXT
);
CREATE INDEX IF NOT EXISTS ix_candidates_name_norm ON candidates(name_norm);

-- 9.3 の4点目「外部と照合する箇所は両表記を試す」ための実測アリアス。
-- DBは正準形でも、画面は別表記で表示されるため。
CREATE TABLE IF NOT EXISTS id_aliases (
  alias         TEXT PRIMARY KEY,
  candidate_id  TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  pattern       TEXT NOT NULL,          -- どの観測パターン由来か
  first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_id_aliases_candidate ON id_aliases(candidate_id);

CREATE TABLE IF NOT EXISTS send_records (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id        TEXT NOT NULL REFERENCES candidates(candidate_id),
  idempotency_key     TEXT NOT NULL,
  message_kind        TEXT NOT NULL CHECK (message_kind IN ('first_contact','follow_up')),
  followup_seq        INTEGER NOT NULL DEFAULT 0,
  -- 9.4: 送信枠は後から絶対に復元できない。読み取り時に endpoint_id から
  -- 導出せず、実測値として保存する (写像自体が変わりうる座標なので、
  -- 導出すると過去行が黙って書き換わる)。'unknown' は一級市民。
  send_slot           TEXT NOT NULL CHECK (send_slot IN ('free','paid','unknown')),
  endpoint_id         TEXT NOT NULL,    -- 'unknown' は許すが空文字は許さない
  -- 13.3: 復元不能。1行目から NOT NULL。
  subject             TEXT NOT NULL,
  subject_norm        TEXT NOT NULL,
  subject_prefix35    TEXT NOT NULL,
  body_digest         TEXT NOT NULL,
  status              TEXT NOT NULL
                      CHECK (status IN ('generated','sending','sent','failed','skipped')),
  -- 9.2: 冪等キーはここでコミットされる。**送信APIを叩く前**。
  reserved_at         TEXT NOT NULL,
  sent_at             TEXT,
  failed_at           TEXT,
  http_status         INTEGER,
  failure_reason      TEXT,
  platform_message_id TEXT,
  attempt_no          INTEGER NOT NULL DEFAULT 1,
  provenance          TEXT NOT NULL,
  run_id              TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_send_idem ON send_records(idempotency_key);
-- コードのバグを生き延びる唯一の二重送信防御。
CREATE UNIQUE INDEX IF NOT EXISTS ux_send_once
  ON send_records(candidate_id, message_kind, followup_seq)
  WHERE status = 'sent';
CREATE INDEX IF NOT EXISTS ix_send_candidate_status ON send_records(candidate_id, status);
-- 「送ったか不明」のまま放置された行を見つけるため (9.2)。
CREATE INDEX IF NOT EXISTS ix_send_stuck ON send_records(status, reserved_at)
  WHERE status = 'sending';
CREATE INDEX IF NOT EXISTS ix_send_subject_norm ON send_records(subject_norm);
CREATE INDEX IF NOT EXISTS ix_send_subject_prefix ON send_records(subject_prefix35);
-- 枠ごとの上限 (9.7) と「合計 == 有料+無料+不明」の恒等式 (9.4) のため。
CREATE INDEX IF NOT EXISTS ix_send_slot_time ON send_records(send_slot, sent_at);

-- dry_run はここにしか書かない。send_records から到達できない。
CREATE TABLE IF NOT EXISTS dry_run_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  candidate_id  TEXT NOT NULL,
  would_slot    TEXT NOT NULL,
  would_endpoint TEXT NOT NULL,
  subject       TEXT NOT NULL,
  body_digest   TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dryrun_run ON dry_run_log(run_id);

-- 9.6: 上限つきバッチをローテーションにする。
-- last_processed_at ASC は SQLite で NULL が先頭に来るので、
-- 「未チェック → 最も昔にチェックしたもの」が索引順でそのまま得られる。
CREATE TABLE IF NOT EXISTS rotation_cursors (
  scope             TEXT NOT NULL,
  candidate_id      TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
  last_processed_at TEXT,
  last_result       TEXT,
  PRIMARY KEY (scope, candidate_id)
);
CREATE INDEX IF NOT EXISTS ix_rotation_order ON rotation_cursors(scope, last_processed_at);

-- 10.4: 誤検知は手作業で消せないので、検知集合を run スコープにして
-- 全量置換 (自己修復) できるようにする。
CREATE TABLE IF NOT EXISTS reply_detection_runs (
  run_id          TEXT PRIMARY KEY,
  started_at      TEXT NOT NULL,
  completed_at    TEXT,
  status          TEXT NOT NULL
                  CHECK (status IN ('building','active','superseded','aborted')),
  page_count      INTEGER NOT NULL DEFAULT 0,
  row_count       INTEGER NOT NULL DEFAULT 0,
  signature_chain TEXT NOT NULL DEFAULT '',
  stop_reason     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_run
  ON reply_detection_runs(status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS reply_detections (
  run_id               TEXT NOT NULL REFERENCES reply_detection_runs(run_id) ON DELETE CASCADE,
  candidate_id         TEXT NOT NULL,
  send_record_id       INTEGER REFERENCES send_records(id),
  matched_subject_norm TEXT NOT NULL,
  match_kind           TEXT NOT NULL CHECK (match_kind IN ('exact','prefix35')),
  replied_at           TEXT,
  source               TEXT NOT NULL,
  evidence_digest      TEXT NOT NULL,
  -- 11.2: 「誰が書いたか」ではなく「何を根拠に書いたか」。
  provenance           TEXT NOT NULL,
  PRIMARY KEY (run_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS ix_detections_candidate ON reply_detections(candidate_id);

-- 曖昧は「照合しない」であって「返信が無い」ではない (10.2)。記録して監視する。
CREATE TABLE IF NOT EXISTS reply_ambiguous (
  run_id        TEXT NOT NULL,
  subject_norm  TEXT NOT NULL,
  candidate_ids TEXT NOT NULL,
  observed_at   TEXT NOT NULL,
  PRIMARY KEY (run_id, subject_norm)
);

CREATE VIEW IF NOT EXISTS v_replies_active AS
  SELECT d.* FROM reply_detections d
  JOIN reply_detection_runs r ON r.run_id = d.run_id AND r.status = 'active';

-- 11.3: 返信は**初回送信**の週・月に帰属させる (コホート方式)。
CREATE VIEW IF NOT EXISTS v_first_send AS
  SELECT candidate_id, MIN(sent_at) AS first_sent_at
  FROM send_records
  WHERE status = 'sent' AND message_kind = 'first_contact'
  GROUP BY candidate_id;

-- 9.9: 追客の送信直前に参照する抑止リスト。媒体側のチェックに依存せず自前でも持つ。
CREATE TABLE IF NOT EXISTS opt_outs (
  candidate_id TEXT PRIMARY KEY,
  reason       TEXT NOT NULL CHECK (reason IN ('replied','declined','opt_out','manual')),
  permanent    INTEGER NOT NULL DEFAULT 0,
  source       TEXT NOT NULL,
  note         TEXT,
  created_at   TEXT NOT NULL
);

-- 9.8: 使わない側のモードは 'closed_other_mode' で明示的に閉じる。
-- 放置すると後日キューが走って二重送信になる。
CREATE TABLE IF NOT EXISTS followups (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id         TEXT NOT NULL REFERENCES candidates(candidate_id),
  first_send_record_id INTEGER NOT NULL REFERENCES send_records(id),
  mode                 TEXT NOT NULL CHECK (mode IN ('platform_native','self_scheduled')),
  scheduled_for        TEXT NOT NULL,
  status               TEXT NOT NULL CHECK (status IN
                         ('scheduled','sent','skipped','suppressed','closed_other_mode')),
  closed_reason        TEXT,
  created_at           TEXT NOT NULL,
  resolved_at          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_followup_once
  ON followups(candidate_id, first_send_record_id, mode);
CREATE INDEX IF NOT EXISTS ix_followup_due ON followups(status, scheduled_for);

-- 12.8: 実行のたびに1行のサマリを出すための集計元。
-- read_errors_skipped は「黙って対象が減る」のを防ぐため必須 (12.5)。
CREATE TABLE IF NOT EXISTS runs (
  run_id              TEXT PRIMARY KEY,
  command             TEXT NOT NULL,
  started_at          TEXT NOT NULL,
  ended_at            TEXT,
  dry_run             INTEGER NOT NULL,
  exit_code           INTEGER,
  targets             INTEGER NOT NULL DEFAULT 0,
  generated_ok        INTEGER NOT NULL DEFAULT 0,
  generation_failed   INTEGER NOT NULL DEFAULT 0,
  sent                INTEGER NOT NULL DEFAULT 0,
  send_failed         INTEGER NOT NULL DEFAULT 0,
  skipped             INTEGER NOT NULL DEFAULT 0,
  read_errors_skipped INTEGER NOT NULL DEFAULT 0,
  wipeout_detected    INTEGER NOT NULL DEFAULT 0
);

-- 13.1: 「1送信あたりの実コスト」を週次で観測できるようにする
-- (参照実装では完全に不可視だった)。
CREATE TABLE IF NOT EXISTS llm_usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  candidate_id  TEXT,
  purpose       TEXT NOT NULL,
  attempt       INTEGER NOT NULL,
  model         TEXT NOT NULL,
  input_tokens  INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  stop_reason   TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_msg ON llm_usage(run_id, candidate_id);

-- 13.2: 偵察の生ダンプは最も機微 (レジュメの生HTML/生JSON・フルページ
-- スクリーンショット)。保持期間を短くし、候補者単位で削除できるようにする。
CREATE TABLE IF NOT EXISTS recon_dumps (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL,
  path         TEXT NOT NULL,
  candidate_id TEXT,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  digest       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_recon_expiry ON recon_dumps(expires_at);
CREATE INDEX IF NOT EXISTS ix_recon_candidate ON recon_dumps(candidate_id);

-- 9.3 の3点目: 起動時マイグレーションの監査証跡。
CREATE TABLE IF NOT EXISTS id_migration_log (
  applied_at           TEXT NOT NULL,
  from_id              TEXT NOT NULL,
  to_id                TEXT NOT NULL,
  pattern              TEXT NOT NULL,
  collision_resolution TEXT
);
