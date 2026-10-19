-- Compartilhar aventura e jogo por turno.
CREATE TABLE IF NOT EXISTS adventure_shares(
  adventure_id TEXT NOT NULL REFERENCES adventures(id) ON DELETE CASCADE,
  username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
  created_at TEXT,
  PRIMARY KEY (adventure_id, username));
ALTER TABLE adventures ADD COLUMN turn_mode INTEGER DEFAULT 0;
ALTER TABLE adventures ADD COLUMN turn_user TEXT DEFAULT '';
