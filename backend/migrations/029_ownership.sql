-- Propriedade de cenários e clones. Registros legados ficam com o admin existente.
ALTER TABLE scenarios ADD COLUMN owner TEXT DEFAULT '';
ALTER TABLE cloned_voices ADD COLUMN owner TEXT DEFAULT '';

UPDATE scenarios SET owner = 'Caio' WHERE owner IS NULL OR owner = '';
UPDATE cloned_voices SET owner = 'Caio' WHERE owner IS NULL OR owner = '';
