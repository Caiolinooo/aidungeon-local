-- Migration 004 (v0.3.0, 2026-10-08): pipeline assíncrono de assets.
-- jobs: fila de geração (worker asyncio em app/jobs.py); story_cards ganham
-- assets por NPC (portrait/voz/vrm); images ganha kind/ref_id para retratos
-- (kind='portrait', ref_id=card_id, action_seq=-1 — ação sequencial não se
-- aplica a retratos, que não aparecem no feed).
-- O runner (backend/app/db.py) aplica quando PRAGMA user_version < 4 e grava
-- user_version=4. ALTER TABLE sem IF NOT EXISTS: o runner checa pragma
-- table_info e pula o statement se a coluna já existir.

CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  type TEXT NOT NULL,                -- 'character_portrait'
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error|canceled
  ref_id TEXT DEFAULT '',            -- story_card alvo
  payload TEXT DEFAULT '{}',         -- JSON de entrada
  result TEXT,                       -- JSON de saída (ex.: {"image": {...}})
  error TEXT,
  created_at TEXT, started_at TEXT, finished_at TEXT);

ALTER TABLE story_cards ADD COLUMN portrait_image_id TEXT;
ALTER TABLE story_cards ADD COLUMN portrait_prompt TEXT DEFAULT '';
ALTER TABLE story_cards ADD COLUMN voice TEXT DEFAULT '';
ALTER TABLE story_cards ADD COLUMN vrm_id TEXT DEFAULT '';

ALTER TABLE images ADD COLUMN kind TEXT DEFAULT 'scene';
ALTER TABLE images ADD COLUMN ref_id TEXT DEFAULT '';
