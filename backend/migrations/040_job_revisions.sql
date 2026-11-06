-- Snapshot the adventure event revision when a media job is enqueued.
ALTER TABLE jobs ADD COLUMN source_revision INTEGER NOT NULL DEFAULT 0;
