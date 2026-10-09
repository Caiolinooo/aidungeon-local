-- A primeira foto do NPC fica como âncora. Retratos seguintes (pedido explícito)
-- são img2img a partir deste arquivo, para o rosto não ser outro personagem.
-- portrait_image_id continua sendo a foto que a UI mostra.

ALTER TABLE story_cards ADD COLUMN portrait_anchor_id TEXT;
