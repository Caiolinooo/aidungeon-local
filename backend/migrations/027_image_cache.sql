-- Imagens: cache por conteúdo e origem do reuso.
-- cache_key é um hash estável de prompt final + config + referências; não
-- contém segredo. reused_from aponta para a imagem que já tinha o arquivo.
ALTER TABLE images ADD COLUMN cache_key TEXT DEFAULT '';
ALTER TABLE images ADD COLUMN reused_from TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_images_cache
  ON images(adventure_id, kind, ref_id, cache_key);
