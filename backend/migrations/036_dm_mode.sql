-- Modo DM: a ação de quem não é dono fica pendente até o dono aprovar.
-- O seq da história só nasce dentro de handle_action, na aprovação.
ALTER TABLE adventures ADD COLUMN dm_mode INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS pending_intents (
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
  username TEXT NOT NULL,
  type TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  action_id INTEGER REFERENCES actions(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_pending_intents_adv
  ON pending_intents(adventure_id, status, created_at);
