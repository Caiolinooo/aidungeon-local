-- Multiplayer ao vivo: quem fez a ação, ficha do jogador e regras de personagem.
ALTER TABLE actions ADD COLUMN actor TEXT DEFAULT '';
ALTER TABLE story_cards ADD COLUMN player_username TEXT DEFAULT '';
ALTER TABLE adventures ADD COLUMN character_rules TEXT DEFAULT '';
