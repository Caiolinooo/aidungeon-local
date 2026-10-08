-- AI Dungeon Local — schema do banco (SQLite, aidungeon.db)
-- Fonte: PLANO.md §4.1 (NÃO alterar campos sem atualizar o plano)

CREATE TABLE scenarios(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
  ai_instructions TEXT DEFAULT '', plot_essentials TEXT DEFAULT '',
  authors_note TEXT DEFAULT '', opening_text TEXT DEFAULT '',
  tags TEXT DEFAULT '[]', third_person INTEGER DEFAULT 0,
  story_cards TEXT DEFAULT '[]',       -- v2: cards do rascunho do assistente (JSON)
  created_at TEXT, updated_at TEXT);

CREATE TABLE adventures(
  id TEXT PRIMARY KEY, scenario_id TEXT REFERENCES scenarios(id),
  title TEXT NOT NULL, description TEXT DEFAULT '',
  ai_instructions TEXT DEFAULT '', plot_essentials TEXT DEFAULT '',
  authors_note TEXT DEFAULT '', story_summary TEXT DEFAULT '',
  tags TEXT DEFAULT '[]', third_person INTEGER DEFAULT 0,
  source TEXT DEFAULT 'new',            -- 'new' | 'import'
  created_at TEXT, updated_at TEXT);

CREATE TABLE actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  seq INTEGER NOT NULL,                  -- ordem absoluta (não o id do backup!)
  type TEXT NOT NULL CHECK(type IN ('start','continue','do','say','story','see')),
  text TEXT NOT NULL, created_at TEXT, edited_at TEXT,
  UNIQUE(adventure_id, seq));

CREATE TABLE story_cards(
  id TEXT PRIMARY KEY, adventure_id TEXT NOT NULL REFERENCES adventures(id),
  title TEXT DEFAULT '', type TEXT DEFAULT 'other',
  triggers TEXT DEFAULT '[]',            -- JSON array de palavras-chave
  entry TEXT DEFAULT '', pinned INTEGER DEFAULT 0,
  use_for_character_creation INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT);

CREATE TABLE memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  text TEXT NOT NULL, seq_start INTEGER, seq_end INTEGER,
  embedding BLOB,                        -- fase 4 (pode ser NULL)
  created_at TEXT);

CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);

-- v2 (2026-10-08): imagens geradas pela ação "See"
CREATE TABLE images(
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

-- v2: estilo visual da aventura (vem de state.imageStyle no backup)
ALTER TABLE adventures ADD COLUMN image_style TEXT DEFAULT '';
