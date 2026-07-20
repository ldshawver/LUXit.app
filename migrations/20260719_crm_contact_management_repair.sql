-- Forward-only repair for partially deployed Contact Management schemas.
-- Every operation is additive/idempotent so existing CRM and communications data is preserved.

ALTER TABLE "user" ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE;
UPDATE "user" SET active = TRUE WHERE active IS NULL;
ALTER TABLE "user" ALTER COLUMN active SET DEFAULT TRUE;
ALTER TABLE "user" ALTER COLUMN active SET NOT NULL;

ALTER TABLE contact ADD COLUMN IF NOT EXISTS normalized_phone VARCHAR(32);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS normalized_email VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS business_name VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS primary_email VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS lifecycle_stage VARCHAR(80) DEFAULT 'new_lead';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS owner_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS status VARCHAR(40) DEFAULT 'active';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS original_source VARCHAR(80);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS latest_source VARCHAR(80);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS google_match_status VARCHAR(50) DEFAULT 'not_checked';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS duplicate_status VARCHAR(50) DEFAULT 'unknown';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS next_follow_up_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS lead_status VARCHAR(80) DEFAULT 'new';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS do_not_contact BOOLEAN DEFAULT FALSE;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS tenant_id INTEGER;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS source_channel VARCHAR(50);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS source_phone_number VARCHAR(32);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS source_provider VARCHAR(80);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS source_context VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP;

UPDATE contact SET lifecycle_stage = 'new_lead' WHERE lifecycle_stage IS NULL;
UPDATE contact SET status = CASE WHEN COALESCE(is_active, TRUE) THEN 'active' ELSE 'archived' END WHERE status IS NULL;
UPDATE contact SET google_match_status = 'not_checked' WHERE google_match_status IS NULL;
UPDATE contact SET duplicate_status = 'unknown' WHERE duplicate_status IS NULL;
UPDATE contact SET lead_status = 'new' WHERE lead_status IS NULL;
UPDATE contact SET do_not_contact = FALSE WHERE do_not_contact IS NULL;
UPDATE contact SET tenant_id = company_id WHERE tenant_id IS NULL AND company_id IS NOT NULL;
ALTER TABLE contact ALTER COLUMN lifecycle_stage SET DEFAULT 'new_lead';
ALTER TABLE contact ALTER COLUMN status SET DEFAULT 'active';
ALTER TABLE contact ALTER COLUMN google_match_status SET DEFAULT 'not_checked';
ALTER TABLE contact ALTER COLUMN duplicate_status SET DEFAULT 'unknown';
ALTER TABLE contact ALTER COLUMN lead_status SET DEFAULT 'new';
ALTER TABLE contact ALTER COLUMN do_not_contact SET DEFAULT FALSE;
ALTER TABLE contact ALTER COLUMN do_not_contact SET NOT NULL;

CREATE TABLE IF NOT EXISTS contact_phone_number (
  id SERIAL PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES company(id),
  contact_id INTEGER NOT NULL REFERENCES contact(id),
  original_value VARCHAR(80), normalized_value VARCHAR(32), extension VARCHAR(30),
  phone_type VARCHAR(40) DEFAULT 'mobile', is_primary BOOLEAN DEFAULT FALSE NOT NULL,
  verification_status VARCHAR(40) DEFAULT 'unverified' NOT NULL, source VARCHAR(80),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contact_email_address (
  id SERIAL PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES company(id),
  contact_id INTEGER NOT NULL REFERENCES contact(id),
  original_value VARCHAR(255), normalized_value VARCHAR(255), email_type VARCHAR(40) DEFAULT 'work',
  is_primary BOOLEAN DEFAULT FALSE NOT NULL, verification_status VARCHAR(40) DEFAULT 'unverified' NOT NULL,
  source VARCHAR(80), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contact_source_event (
  id SERIAL PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES company(id),
  contact_id INTEGER NOT NULL REFERENCES contact(id),
  source VARCHAR(80) NOT NULL, source_detail VARCHAR(255), campaign VARCHAR(255),
  source_url VARCHAR(500), referrer VARCHAR(500), event_type VARCHAR(80) DEFAULT 'touch' NOT NULL,
  event_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL, metadata JSON,
  created_by_user_id INTEGER REFERENCES "user"(id)
);

CREATE TABLE IF NOT EXISTS opportunity (
  id SERIAL PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES company(id),
  contact_id INTEGER NOT NULL REFERENCES contact(id),
  owner_user_id INTEGER REFERENCES "user"(id), name VARCHAR(255) NOT NULL,
  pipeline VARCHAR(80) DEFAULT 'sales' NOT NULL, stage VARCHAR(80) DEFAULT 'new_lead' NOT NULL,
  estimated_value NUMERIC(12,2), probability INTEGER DEFAULT 0 NOT NULL,
  expected_close_date DATE, status VARCHAR(40) DEFAULT 'open' NOT NULL,
  won_lost_reason VARCHAR(255), next_action VARCHAR(255), follow_up_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_phone ON contact(company_id, normalized_phone) WHERE normalized_phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_email ON contact(company_id, normalized_email) WHERE normalized_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contact_company_lifecycle ON contact(company_id, lifecycle_stage);
CREATE INDEX IF NOT EXISTS ix_contact_company_owner ON contact(company_id, owner_user_id);
CREATE INDEX IF NOT EXISTS ix_contact_company_followup ON contact(company_id, next_follow_up_at);
CREATE INDEX IF NOT EXISTS ix_contact_phone_company_norm ON contact_phone_number(company_id, normalized_value) WHERE normalized_value IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_phone_primary ON contact_phone_number(contact_id) WHERE is_primary = TRUE;
CREATE INDEX IF NOT EXISTS ix_contact_email_company_norm ON contact_email_address(company_id, normalized_value) WHERE normalized_value IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contact_source_event_company_contact ON contact_source_event(company_id, contact_id, event_at);
CREATE INDEX IF NOT EXISTS ix_contact_source_event_source ON contact_source_event(company_id, source);
CREATE INDEX IF NOT EXISTS ix_opportunity_company_stage ON opportunity(company_id, stage, status);
CREATE INDEX IF NOT EXISTS ix_opportunity_contact ON opportunity(contact_id);
