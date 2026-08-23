-- 画面に出る会員番号を保存する。
--
-- 取り込みと生成は **別のプロセス** である (scout ingest && scout generate)。
-- 途中で持ち回せるのはDBだけなので、宛名に使う番号はここに残さなければ
-- 生成の時点で消えている。
--
-- **candidate_id とは別物である。** candidate_id は API が使う内部の番号
-- (members[].id) で、member_code は運用者と候補者が画面で目にする番号
-- (members[].code) である。実測では id=3323741 に対して code="01613058" だった。
--
-- **NULL を許す。** 取れない候補者が居ても取り込みを止めない -- 止めると
-- 「1件も取れなかった」ことになり、原則2 の静かなゼロ件に近づく。
-- 宛名に使えるかどうかは generation.scout_body が本文を見て判定する。
ALTER TABLE candidates ADD COLUMN member_code TEXT;

CREATE INDEX IF NOT EXISTS ix_candidates_member_code ON candidates(member_code);
