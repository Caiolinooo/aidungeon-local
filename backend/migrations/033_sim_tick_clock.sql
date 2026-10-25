-- Relógio real do último tick, para o catch-up depois de uma pausa do processo.
ALTER TABLE sim_runtime ADD COLUMN last_tick_real REAL NOT NULL DEFAULT 0;
