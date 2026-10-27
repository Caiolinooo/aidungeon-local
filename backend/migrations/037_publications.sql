-- Publicações: catálogo, versões imutáveis e linhagem de remix.
-- Banco novo marca esta migration como aplicada sem executá-la (o DDL
-- equivalente entra pelo schema na integração do owner). ensure_schema
-- aplica este arquivo se a tabela ainda não existe.
--
-- Os triggers publication_versions_immutable (UPDATE) e
-- publication_versions_no_delete (DELETE) são criados por
-- app.publications.ensure_schema. O corpo de um trigger precisa de ';',
-- e o runner de migrations parte o arquivo em todo ';'.

CREATE TABLE IF NOT EXISTS publications(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('scenario', 'adventure')),
  source_id TEXT NOT NULL,
  owner TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  cover_image TEXT DEFAULT '',
  tags TEXT DEFAULT '[]',
  latest_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_versions(
  publication_id TEXT NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  snapshot TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  cover_image TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (publication_id, version)
);

CREATE TABLE IF NOT EXISTS publication_likes(
  publication_id TEXT NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
  username TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (publication_id, username)
);

-- Linha presente: apagar uma versão isolada aborta.
-- delete_publication remove a linha, apaga as versões e a reinsere.
CREATE TABLE IF NOT EXISTS publication_version_delete_guard(
  id INTEGER PRIMARY KEY CHECK (id = 1)
);

INSERT OR IGNORE INTO publication_version_delete_guard(id) VALUES (1);

CREATE INDEX IF NOT EXISTS idx_publications_updated
  ON publications(updated_at);

CREATE INDEX IF NOT EXISTS idx_publications_source
  ON publications(kind, source_id, owner);

CREATE INDEX IF NOT EXISTS idx_publication_versions_hash
  ON publication_versions(publication_id, content_hash);

ALTER TABLE scenarios ADD COLUMN parent_publication_id TEXT REFERENCES publications(id) ON DELETE SET NULL;

ALTER TABLE scenarios ADD COLUMN parent_version INTEGER;

ALTER TABLE adventures ADD COLUMN publication_id TEXT REFERENCES publications(id) ON DELETE SET NULL;

ALTER TABLE adventures ADD COLUMN publication_version INTEGER;
