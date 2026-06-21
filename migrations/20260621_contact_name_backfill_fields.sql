-- Additive contact cache fields needed for Google/CRM contact-name backfill.
-- Safe to run repeatedly; additive only and preserves existing contact data.

ALTER TABLE contact ADD COLUMN IF NOT EXISTS name VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS normalized_phone VARCHAR(32);
CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_phone ON contact(company_id, normalized_phone);
