-- Fatos duráveis do mundo (030). O contador participa da mesma transação do
-- evento para manter event_id crescente por aventura sem depender do relógio.
CREATE TABLE IF NOT EXISTS world_events (
  adventure_id TEXT NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL,
  state_revision INTEGER NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  actor TEXT NOT NULL DEFAULT '',
  entity_id TEXT NOT NULL DEFAULT '',
  turn_seq INTEGER,
  generation_id TEXT NOT NULL DEFAULT '',
  correlation_id TEXT NOT NULL DEFAULT '',
  causation_id INTEGER,
  sim_day INTEGER,
  sim_minute INTEGER,
  created_at TEXT NOT NULL,
  idempotency_key TEXT,
  PRIMARY KEY (adventure_id, event_id),
  UNIQUE (adventure_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_world_events_replay
  ON world_events(adventure_id, event_id);
CREATE INDEX IF NOT EXISTS idx_world_events_causation
  ON world_events(adventure_id, causation_id);

CREATE TABLE IF NOT EXISTS world_event_counters (
  adventure_id TEXT PRIMARY KEY REFERENCES adventures(id) ON DELETE CASCADE,
  last_event_id INTEGER NOT NULL DEFAULT 0,
  state_revision INTEGER NOT NULL DEFAULT 0
);
