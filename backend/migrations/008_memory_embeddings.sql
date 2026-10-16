-- Migration 008 (Milestone 2, R1 Phase 4): embeddings de memória na CPU.
-- O runner (backend/app/db.py) aplica quando PRAGMA user_version < 8
-- e grava user_version=8. ALTER TABLE sem IF NOT EXISTS: o runner checa
-- pragma table_info(memories) e pula o statement se a coluna já existir.

-- embedding_model: identificador do modelo que gerou o vetor (ex.: "Qwen3-Embedding-0.6B")
ALTER TABLE memories ADD COLUMN embedding_model TEXT;

-- embedding_dim: dimensionalidade do vetor (ex.: 1024)
ALTER TABLE memories ADD COLUMN embedding_dim INTEGER;
