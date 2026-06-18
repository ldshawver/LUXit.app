-- Segment administration, membership provenance, and marketing suppression fields.
ALTER TABLE contact ADD COLUMN IF NOT EXISTS do_not_market BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS do_not_email BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS do_not_sms BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS email_unsubscribed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS sms_opted_out BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS marketing_preferences_reason VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS marketing_preferences_source VARCHAR(120);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS marketing_preferences_updated_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS marketing_preferences_updated_at TIMESTAMP;

ALTER TABLE segment ADD COLUMN IF NOT EXISTS category VARCHAR(120);
ALTER TABLE segment ADD COLUMN IF NOT EXISTS match_mode VARCHAR(20) NOT NULL DEFAULT 'all';
ALTER TABLE segment ADD COLUMN IF NOT EXISTS triggers JSON;
ALTER TABLE segment ADD COLUMN IF NOT EXISTS actions JSON;
ALTER TABLE segment ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE segment_member ADD COLUMN IF NOT EXISTS source VARCHAR(80) NOT NULL DEFAULT 'manual';
ALTER TABLE segment_member ADD COLUMN IF NOT EXISTS added_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE segment_member ADD COLUMN IF NOT EXISTS removed_at TIMESTAMP;
ALTER TABLE segment_member ADD COLUMN IF NOT EXISTS removed_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE segment_member ADD COLUMN IF NOT EXISTS exclusion_reason VARCHAR(255);
ALTER TABLE segment_member ADD COLUMN IF NOT EXISTS is_excluded BOOLEAN NOT NULL DEFAULT FALSE;
CREATE UNIQUE INDEX IF NOT EXISTS uq_segment_member_contact ON segment_member(segment_id, contact_id);
CREATE INDEX IF NOT EXISTS ix_segment_member_segment_contact ON segment_member(segment_id, contact_id);
