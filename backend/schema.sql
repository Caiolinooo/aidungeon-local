-- AI Dungeon Local — schema do banco (SQLite, aidungeon.db)
-- Fonte: PLANO.md §4.1 (NÃO alterar campos sem atualizar o plano)

CREATE TABLE scenarios(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
  ai_instructions TEXT DEFAULT '', plot_essentials TEXT DEFAULT '',
  authors_note TEXT DEFAULT '', opening_text TEXT DEFAULT '',
  image_style TEXT DEFAULT '',
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
  ui_profile TEXT DEFAULT '',           -- v0.7.0: manifesto JSON de controles (IA)
  ui_state TEXT DEFAULT '',             -- v0.7.0: estado JSON dos widgets
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
  embedding_model TEXT,                  -- fase 4: nome do modelo (ex.: Qwen3-Embedding-0.6B)
  embedding_dim INTEGER,                 -- fase 4: dimensão do vetor (ex.: 1024)
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

-- v0.3.0 (2026-10-08): assets por NPC nos story cards
ALTER TABLE story_cards ADD COLUMN portrait_image_id TEXT;
ALTER TABLE story_cards ADD COLUMN portrait_prompt TEXT DEFAULT '';
ALTER TABLE story_cards ADD COLUMN voice TEXT DEFAULT '';
ALTER TABLE story_cards ADD COLUMN vrm_id TEXT DEFAULT '';

-- v0.5.0 (2026-10-09): ficha estruturada do NPC (JSON: appearance, body,
-- clothing, personality, stats, notes) — alimenta retratos e a aba NPCs
ALTER TABLE story_cards ADD COLUMN sheet TEXT DEFAULT '';

-- Retrato de identidade: a primeira foto do NPC. Gerações seguintes (img2img)
-- partem deste arquivo; geração automática não substitui portrait_image_id.
ALTER TABLE story_cards ADD COLUMN portrait_anchor_id TEXT;

-- v0.3.0: kind='portrait' + ref_id=card_id para retratos (action_seq=-1: retrato
-- não pertence ao feed de ações)
ALTER TABLE images ADD COLUMN kind TEXT DEFAULT 'scene';
ALTER TABLE images ADD COLUMN ref_id TEXT DEFAULT '';

-- v0.3.0: fila do pipeline assíncrono de assets (worker em app/jobs.py)
CREATE TABLE jobs(
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  type TEXT NOT NULL,                -- 'character_portrait'
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error|canceled
  ref_id TEXT DEFAULT '',            -- story_card alvo
  payload TEXT DEFAULT '{}',         -- JSON de entrada
  result TEXT,                       -- JSON de saída (ex.: {"image": {...}})
  error TEXT,
  created_at TEXT, started_at TEXT, finished_at TEXT);

-- Donos: Caio é o admin. Cada aventura pertence a um usuário.
CREATE TABLE users(
  username TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT);
ALTER TABLE adventures ADD COLUMN owner TEXT DEFAULT '';

-- --- Grok (G5): jobs com prioridade e chave de substituição (C5, migration 021)
ALTER TABLE jobs ADD COLUMN priority INTEGER DEFAULT 2;
ALTER TABLE jobs ADD COLUMN supersede_key TEXT DEFAULT '';
CREATE INDEX jobs_queue ON jobs(status, priority, created_at);
