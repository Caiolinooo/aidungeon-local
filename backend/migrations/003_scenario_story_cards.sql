-- Migration 003 (v2, 2026-10-08): story cards do rascunho do assistente no scenario.
-- O runner (backend/app/db.py) aplica quando PRAGMA user_version < 3 e grava
-- user_version=3. ALTER TABLE sem IF NOT EXISTS: o runner checa pragma
-- table_info(scenarios) e pula o statement se a coluna já existir.

ALTER TABLE scenarios ADD COLUMN story_cards TEXT DEFAULT '[]';
