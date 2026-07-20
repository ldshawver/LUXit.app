-- Additive, rerunnable contact identity workflow state. No customer data is rewritten.
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_status VARCHAR(32) NOT NULL DEFAULT 'pending_identity';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS pending_first_name VARCHAR(120);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS pending_last_name VARCHAR(120);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS pending_email VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_requested_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_request_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_confirmed_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_confirmation_sid VARCHAR(64);
CREATE INDEX IF NOT EXISTS ix_contact_company_identity_status ON contact(company_id, identity_status);

ALTER TABLE company ADD COLUMN IF NOT EXISTS sync_confirmed_contacts_to_google BOOLEAN NOT NULL DEFAULT FALSE;
