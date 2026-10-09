-- v0.5.0: ficha estruturada do NPC (story_cards.sheet, JSON). Campos livres
-- mas convencionados (ver docs/openapi.yaml): appearance, body, personality,
-- stats {str,dex,con,int,wis,cha}, notes. Alimenta o prompt de retrato e a
-- UI da aba NPCs; '' = sem ficha (cards legados).

ALTER TABLE story_cards ADD COLUMN sheet TEXT DEFAULT '';
