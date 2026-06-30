-- Track where and when contacts were added, and support duplicate merge audits.
ALTER TABLE contact ADD COLUMN IF NOT EXISTS source_detail VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS source_added_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS source_added_by_user_id INTEGER REFERENCES "user"(id);

UPDATE contact
SET source_added_at = COALESCE(source_added_at, created_at)
WHERE source_added_at IS NULL;
