-- v0.9.0: Direção visual (image_style) persistida diretamente no Scenario
ALTER TABLE scenarios ADD COLUMN image_style TEXT DEFAULT '';
