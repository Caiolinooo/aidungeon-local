-- Migration 007 (v0.7.0, 2026-10-13): painel de controles dinâmico definido
-- pela IA. O runner (backend/app/db.py) aplica quando PRAGMA user_version < 7
-- e grava user_version=7. ALTER TABLE sem IF NOT EXISTS: o runner checa
-- pragma table_info(adventures) e pula o statement se a coluna já existir.

-- ui_profile: manifesto JSON gerado pelo LLM descrevendo os controles do
-- jogo (tabs do InputBar + widgets: dice/stats/inventory/character).
-- '' = ainda não gerado.
ALTER TABLE adventures ADD COLUMN ui_profile TEXT DEFAULT '';

-- ui_state: estado JSON dos widgets (HP atual, itens do inventário, campos
-- da ficha) editado pelo jogador no painel. '{}' = vazio.
ALTER TABLE adventures ADD COLUMN ui_state TEXT DEFAULT '';
