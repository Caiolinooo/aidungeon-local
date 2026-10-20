CREATE TABLE IF NOT EXISTS npc_memories (
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL,
  card_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'event',
  text TEXT NOT NULL,
  mood TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_npc_memories_card ON npc_memories(adventure_id, card_id, created_at);
ALTER TABLE adventures ADD COLUMN life_day INTEGER NOT NULL DEFAULT 0;
ALTER TABLE adventures ADD COLUMN life_tick_seq INTEGER NOT NULL DEFAULT 0;
