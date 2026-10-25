CREATE TABLE IF NOT EXISTS sim_runtime(
  adventure_id TEXT PRIMARY KEY REFERENCES adventures(id) ON DELETE CASCADE,
  running INTEGER NOT NULL DEFAULT 0,
  speed INTEGER NOT NULL DEFAULT 1 CHECK(speed IN (1, 4, 20)),
  tick_seq INTEGER NOT NULL DEFAULT 0,
  active_tick_id TEXT NOT NULL DEFAULT '',
  lease_owner TEXT NOT NULL DEFAULT '',
  lease_until REAL NOT NULL DEFAULT 0,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sim_ticks(
  adventure_id TEXT NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
  tick_id TEXT NOT NULL,
  day INTEGER NOT NULL,
  minute INTEGER NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT,
  PRIMARY KEY(adventure_id, tick_id)
);
