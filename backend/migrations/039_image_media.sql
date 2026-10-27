-- Clipe Wan: a tabela images também guarda MP4.
-- Banco novo: schema.sql já cria as colunas; o init só marca este número.
-- Banco existente: adiciona o que ainda não existe (o runner ignora coluna repetida).

ALTER TABLE images ADD COLUMN media_type TEXT DEFAULT 'image/png';
ALTER TABLE images ADD COLUMN duration_ms INTEGER;
ALTER TABLE images ADD COLUMN source_image_id TEXT DEFAULT '';
