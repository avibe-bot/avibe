ALTER TABLE memory_meta ADD COLUMN last_error_at TEXT;

UPDATE memory_meta
SET last_error_at = updated_at
WHERE last_error IS NOT NULL;
