-- Vozes clonadas pelo jogador (áudio enviado ou gravado).
-- O rvq é a referência que o OmniVoice usa; o arquivo na VM é regravado a cada fala.
CREATE TABLE IF NOT EXISTS cloned_voices (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  ref_text TEXT NOT NULL,
  rvq BLOB NOT NULL,
  sample_seconds REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cloned_voices_name
  ON cloned_voices(name COLLATE NOCASE);
