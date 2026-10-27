-- Inícios nomeados do scenario. O texto legado fica em opening_text;
-- o backfill em db.init_db copia esse texto para o início padrão.
ALTER TABLE scenarios ADD COLUMN story_starts TEXT DEFAULT '[]';
