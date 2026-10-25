-- Snapshot lógico anterior a cada geração. O undo restaura esta foto,
-- não só o texto das actions.
CREATE TABLE IF NOT EXISTS turn_canon (
  adventure_id TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  anchor_action_id INTEGER,
  snapshot_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (adventure_id, generation_id)
);
CREATE INDEX IF NOT EXISTS idx_turn_canon_anchor
  ON turn_canon(adventure_id, anchor_action_id);
