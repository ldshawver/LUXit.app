-- Contact Intelligence, Deduplication, Google Contacts, Source Attribution, and Sales CRM
-- Additive migration only. Cleanup/backfill jobs are deliberately not run by this migration.

ALTER TABLE contact ADD COLUMN IF NOT EXISTS display_name VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS business_name VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS primary_phone VARCHAR(50);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS phone_extension VARCHAR(30);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS primary_email VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS normalized_email VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS contact_type VARCHAR(50);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS lifecycle_stage VARCHAR(80) DEFAULT 'new_lead';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS owner_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS status VARCHAR(40) DEFAULT 'active';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS original_source VARCHAR(80);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS latest_source VARCHAR(80);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS original_source_detail VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS latest_source_detail VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS original_source_campaign VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS latest_source_campaign VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS original_source_url VARCHAR(500);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS latest_source_url VARCHAR(500);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS original_referrer VARCHAR(500);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS latest_referrer VARCHAR(500);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS first_touch_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS last_touch_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS name_source VARCHAR(80) DEFAULT 'user';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS google_contact_resource_id VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS google_contact_etag VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS google_match_status VARCHAR(50) DEFAULT 'not_checked';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS google_match_confidence INTEGER;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS google_matched_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS google_sync_status VARCHAR(50);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS google_sync_error TEXT;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS google_name_last_checked_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS duplicate_status VARCHAR(50) DEFAULT 'unknown';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS possible_duplicate_of_id INTEGER REFERENCES contact(id);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS duplicate_confidence INTEGER;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS duplicate_reason VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS merged_into_contact_id INTEGER REFERENCES contact(id);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS merged_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS merged_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS lead_status VARCHAR(80) DEFAULT 'new';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS lead_score INTEGER DEFAULT 0;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS estimated_value NUMERIC(12,2);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS last_contacted_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS next_follow_up_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS won_revenue NUMERIC(12,2);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS lost_reason VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS do_not_contact BOOLEAN DEFAULT FALSE NOT NULL;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS email_consent_status VARCHAR(30) DEFAULT 'unknown';

CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_phone ON contact(company_id, normalized_phone) WHERE normalized_phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_email ON contact(company_id, normalized_email) WHERE normalized_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contact_company_lifecycle ON contact(company_id, lifecycle_stage);
CREATE INDEX IF NOT EXISTS ix_contact_company_owner ON contact(company_id, owner_user_id);
CREATE INDEX IF NOT EXISTS ix_contact_company_followup ON contact(company_id, next_follow_up_at);

CREATE TABLE IF NOT EXISTS contact_phone_number (
  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id), contact_id INTEGER NOT NULL REFERENCES contact(id),
  original_value VARCHAR(80), normalized_value VARCHAR(32), extension VARCHAR(30), phone_type VARCHAR(40) DEFAULT 'mobile',
  is_primary BOOLEAN DEFAULT FALSE NOT NULL, verification_status VARCHAR(40) DEFAULT 'unverified' NOT NULL, source VARCHAR(80),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_contact_phone_company_norm ON contact_phone_number(company_id, normalized_value) WHERE normalized_value IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_phone_primary ON contact_phone_number(contact_id) WHERE is_primary = TRUE;

CREATE TABLE IF NOT EXISTS contact_email_address (
  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id), contact_id INTEGER NOT NULL REFERENCES contact(id),
  original_value VARCHAR(255), normalized_value VARCHAR(255), email_type VARCHAR(40) DEFAULT 'work', is_primary BOOLEAN DEFAULT FALSE NOT NULL,
  verification_status VARCHAR(40) DEFAULT 'unverified' NOT NULL, source VARCHAR(80), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_contact_email_company_norm ON contact_email_address(company_id, normalized_value) WHERE normalized_value IS NOT NULL;

CREATE TABLE IF NOT EXISTS contact_source_event (
  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id), contact_id INTEGER NOT NULL REFERENCES contact(id), source VARCHAR(80) NOT NULL,
  source_detail VARCHAR(255), campaign VARCHAR(255), source_url VARCHAR(500), referrer VARCHAR(500), event_type VARCHAR(80) DEFAULT 'touch' NOT NULL,
  event_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL, metadata JSON, created_by_user_id INTEGER REFERENCES "user"(id)
);
CREATE INDEX IF NOT EXISTS ix_contact_source_event_company_contact ON contact_source_event(company_id, contact_id, event_at);
CREATE INDEX IF NOT EXISTS ix_contact_source_event_source ON contact_source_event(company_id, source);

CREATE TABLE IF NOT EXISTS google_contact_connection (
  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id), user_id INTEGER NOT NULL REFERENCES "user"(id),
  google_account_email VARCHAR(255), encrypted_refresh_token_ref TEXT, scopes JSON, sync_status VARCHAR(50) DEFAULT 'disconnected' NOT NULL,
  last_successful_sync_at TIMESTAMP, last_failure_at TIMESTAMP, last_error TEXT, disconnected_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_google_contact_connection_owner ON google_contact_connection(company_id, user_id);

CREATE TABLE IF NOT EXISTS opportunity (
  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id), contact_id INTEGER NOT NULL REFERENCES contact(id), owner_user_id INTEGER REFERENCES "user"(id),
  name VARCHAR(255) NOT NULL, pipeline VARCHAR(80) DEFAULT 'sales' NOT NULL, stage VARCHAR(80) DEFAULT 'new_lead' NOT NULL,
  estimated_value NUMERIC(12,2), probability INTEGER DEFAULT 0 NOT NULL, expected_close_date DATE, status VARCHAR(40) DEFAULT 'open' NOT NULL,
  won_lost_reason VARCHAR(255), next_action VARCHAR(255), follow_up_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_opportunity_company_stage ON opportunity(company_id, stage, status);
CREATE INDEX IF NOT EXISTS ix_opportunity_contact ON opportunity(contact_id);

CREATE TABLE IF NOT EXISTS contact_task (
  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id), contact_id INTEGER NOT NULL REFERENCES contact(id), assigned_user_id INTEGER REFERENCES "user"(id),
  title VARCHAR(255) NOT NULL, due_at TIMESTAMP, priority VARCHAR(30) DEFAULT 'normal' NOT NULL, status VARCHAR(40) DEFAULT 'open' NOT NULL,
  reminder_at TIMESTAMP, completed_at TIMESTAMP, completed_by_user_id INTEGER REFERENCES "user"(id), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_contact_task_company_due ON contact_task(company_id, due_at, status);

CREATE TABLE IF NOT EXISTS contact_intelligence_job (
  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id), user_id INTEGER REFERENCES "user"(id), job_type VARCHAR(80) NOT NULL,
  status VARCHAR(40) DEFAULT 'queued' NOT NULL, cursor VARCHAR(255), batch_size INTEGER DEFAULT 100 NOT NULL,
  total_found INTEGER DEFAULT 0 NOT NULL, processed INTEGER DEFAULT 0 NOT NULL, updated INTEGER DEFAULT 0 NOT NULL, skipped INTEGER DEFAULT 0 NOT NULL,
  ambiguous INTEGER DEFAULT 0 NOT NULL, failed INTEGER DEFAULT 0 NOT NULL, dry_run BOOLEAN DEFAULT TRUE NOT NULL, checkpoint JSON, failures JSON,
  sanitized_last_error TEXT, started_at TIMESTAMP, completed_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_contact_intel_job_company_type ON contact_intelligence_job(company_id, job_type, status);

CREATE TABLE IF NOT EXISTS google_contact_lookup (
  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id), user_id INTEGER NOT NULL REFERENCES "user"(id),
  connection_id INTEGER REFERENCES google_contact_connection(id), normalized_phone VARCHAR(32) NOT NULL, display_name VARCHAR(255),
  resource_id VARCHAR(255), etag VARCHAR(255), is_ambiguous BOOLEAN DEFAULT FALSE NOT NULL, candidate_count INTEGER DEFAULT 1 NOT NULL,
  candidates JSON, last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_google_lookup_company_phone ON google_contact_lookup(company_id, normalized_phone);
CREATE INDEX IF NOT EXISTS ix_google_lookup_user_phone ON google_contact_lookup(user_id, normalized_phone);

CREATE TABLE IF NOT EXISTS contact_duplicate_exclusion (
  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id), contact_id_a INTEGER NOT NULL REFERENCES contact(id),
  contact_id_b INTEGER NOT NULL REFERENCES contact(id), reason VARCHAR(255), created_by_user_id INTEGER REFERENCES "user"(id),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_contact_duplicate_exclusion_pair ON contact_duplicate_exclusion(company_id, contact_id_a, contact_id_b);
