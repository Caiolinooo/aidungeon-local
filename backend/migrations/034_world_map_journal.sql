-- Mapa vivo, journal cruzado e imagens de locais.
-- O mundo usa cards existentes; estas tabelas guardam relações e navegação.

CREATE TABLE IF NOT EXISTS location_links(
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
  from_card_id TEXT NOT NULL REFERENCES story_cards(id) ON DELETE CASCADE,
  to_card_id TEXT NOT NULL REFERENCES story_cards(id) ON DELETE CASCADE,
  travel_text TEXT DEFAULT '',
  travel_cost TEXT DEFAULT '',
  bidirectional INTEGER DEFAULT 1,
  discovered INTEGER DEFAULT 1,
  created_at TEXT,
  UNIQUE(adventure_id, from_card_id, to_card_id)
);
CREATE INDEX IF NOT EXISTS idx_location_links_adventure
  ON location_links(adventure_id, discovered);

CREATE TABLE IF NOT EXISTS map_state(
  adventure_id TEXT PRIMARY KEY REFERENCES adventures(id) ON DELETE CASCADE,
  current_card_id TEXT REFERENCES story_cards(id) ON DELETE SET NULL,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS journal_entries(
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
  action_id INTEGER REFERENCES actions(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  entity_type TEXT DEFAULT '',
  entity_id TEXT DEFAULT '',
  related_json TEXT DEFAULT '[]',
  visibility TEXT DEFAULT 'public',
  created_at TEXT,
  UNIQUE(adventure_id, action_id, kind, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_journal_adventure
  ON journal_entries(adventure_id, created_at DESC);

CREATE TABLE IF NOT EXISTS location_images(
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
  card_id TEXT NOT NULL REFERENCES story_cards(id) ON DELETE CASCADE,
  image_id TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  state_key TEXT DEFAULT '',
  created_at TEXT,
  UNIQUE(adventure_id, card_id, state_key)
);
