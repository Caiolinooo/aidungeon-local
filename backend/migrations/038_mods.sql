-- Mods de aventura (catálogo + versões imutáveis + aplicação por save).
-- Banco novo: o init marca este número sem executar o arquivo. O mesmo DDL
-- precisa entrar em schema.sql (executescript). Banco já existente roda aqui.
--
-- Triggers de imutabilidade não cabem neste runner (ele parte o arquivo no ';').
-- O módulo app.mods instala os triggers no primeiro uso da API. No schema.sql:
-- CREATE TRIGGER IF NOT EXISTS mod_versions_no_update
-- BEFORE UPDATE ON mod_versions
-- BEGIN
--   SELECT RAISE(ABORT, 'mod_versions are immutable');
-- END;
-- CREATE TRIGGER IF NOT EXISTS mod_versions_no_delete
-- BEFORE DELETE ON mod_versions
-- BEGIN
--   SELECT RAISE(ABORT, 'mod_versions are immutable');
-- END;

CREATE TABLE IF NOT EXISTS mods(
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  visibility TEXT NOT NULL DEFAULT 'public' CHECK(visibility IN ('public', 'private')),
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS mod_versions(
  id TEXT PRIMARY KEY,
  mod_id TEXT NOT NULL REFERENCES mods(id),
  version INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  package_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(mod_id, version)
);

CREATE TABLE IF NOT EXISTS adventure_mods(
  adventure_id TEXT NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
  mod_version_id TEXT NOT NULL REFERENCES mod_versions(id),
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  applied_at TEXT NOT NULL,
  PRIMARY KEY(adventure_id, mod_version_id)
);

ALTER TABLE story_cards ADD COLUMN origin_mod_version_id TEXT REFERENCES mod_versions(id);

ALTER TABLE location_links ADD COLUMN origin_mod_version_id TEXT REFERENCES mod_versions(id);

CREATE INDEX IF NOT EXISTS idx_mods_owner ON mods(owner, updated_at);
CREATE INDEX IF NOT EXISTS idx_mod_versions_mod ON mod_versions(mod_id, version);
CREATE INDEX IF NOT EXISTS idx_adventure_mods_adv ON adventure_mods(adventure_id, enabled);
CREATE INDEX IF NOT EXISTS idx_story_cards_mod_origin ON story_cards(adventure_id, origin_mod_version_id);
CREATE INDEX IF NOT EXISTS idx_location_links_mod_origin ON location_links(adventure_id, origin_mod_version_id);
