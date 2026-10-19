-- Usuários e dono da aventura. Histórias já existentes ficam com o Caio.
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT);

ALTER TABLE adventures ADD COLUMN owner TEXT DEFAULT '';

UPDATE adventures SET owner = 'Caio' WHERE owner IS NULL OR owner = '';
