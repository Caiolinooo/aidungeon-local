-- Migration 002 (v2, 2026-10-08): imagens da ação "See" + image_style da aventura.
-- O runner (backend/app/db.py) deve: checar PRAGMA user_version; se < 2, aplicar
-- este arquivo e gravar user_version=2. Statements idempotentes onde possível.

CREATE TABLE IF NOT EXISTS images(
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  action_seq INTEGER NOT NULL,
  prompt TEXT DEFAULT '',
  negative_prompt TEXT DEFAULT '',
  file_path TEXT NOT NULL,
  provider TEXT DEFAULT '',
  width INTEGER,
  height INTEGER,
  created_at TEXT);

-- ALTER TABLE não tem IF NOT EXISTS: o runner deve checar pragma table_info(adventures)
-- e só executar se a coluna image_style ainda não existir.
ALTER TABLE adventures ADD COLUMN image_style TEXT DEFAULT '';
