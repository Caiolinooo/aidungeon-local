-- Jobs com prioridade (menor roda antes) e chave de substituição (contrato C5).
-- Job novo com a mesma supersede_key cancela os pendentes antigos. Status
-- novo 'preparing': o See espera o prompt do LLM fora da fila do SD.
ALTER TABLE jobs ADD COLUMN priority INTEGER DEFAULT 2;
ALTER TABLE jobs ADD COLUMN supersede_key TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, priority, created_at);
