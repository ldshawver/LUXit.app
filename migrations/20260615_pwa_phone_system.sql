-- PWA phone system schema migration
-- Safe for existing PostgreSQL production databases; every object is guarded.

BEGIN;

ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS parent_call_sid VARCHAR(100);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS forwarded_to_number VARCHAR(20);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS contact_id INTEGER;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS customer_id INTEGER;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS assigned_user_id INTEGER;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS answered_by_user_id INTEGER;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS answered_at TIMESTAMP;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS ended_at TIMESTAMP;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS recording_url VARCHAR(500);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS recording_sid VARCHAR(100);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS voicemail_url VARCHAR(500);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS voicemail_sid VARCHAR(100);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS transcription_text TEXT;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS transcription_status VARCHAR(30) DEFAULT 'not_requested';
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS transcription_provider VARCHAR(80);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS metadata_json JSONB;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS is_read BOOLEAN DEFAULT FALSE;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW();

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_twilio_call_log_contact') THEN
    ALTER TABLE twilio_call_log ADD CONSTRAINT fk_twilio_call_log_contact FOREIGN KEY (contact_id) REFERENCES contact(id) ON DELETE SET NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_twilio_call_log_assigned_user') THEN
    ALTER TABLE twilio_call_log ADD CONSTRAINT fk_twilio_call_log_assigned_user FOREIGN KEY (assigned_user_id) REFERENCES "user"(id) ON DELETE SET NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_twilio_call_log_answered_user') THEN
    ALTER TABLE twilio_call_log ADD CONSTRAINT fk_twilio_call_log_answered_user FOREIGN KEY (answered_by_user_id) REFERENCES "user"(id) ON DELETE SET NULL;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS phone_settings (
  id SERIAL PRIMARY KEY,
  company_id INTEGER NOT NULL UNIQUE REFERENCES company(id) ON DELETE CASCADE,
  business_hours JSONB DEFAULT '{}'::jsonb,
  timezone VARCHAR(80) DEFAULT 'America/Los_Angeles',
  during_hours_route VARCHAR(30) DEFAULT 'ring_pwa',
  after_hours_route VARCHAR(30) DEFAULT 'voicemail',
  forward_number VARCHAR(20),
  fallback_forward_number VARCHAR(20),
  after_hours_forward_number VARCHAR(20),
  after_hours_fallback_forward_number VARCHAR(20),
  ring_duration_seconds INTEGER DEFAULT 25,
  voicemail_greeting TEXT,
  after_hours_voicemail_greeting TEXT,
  missed_call_sms_enabled BOOLEAN DEFAULT FALSE,
  missed_call_sms_body TEXT,
  after_hours_sms_enabled BOOLEAN DEFAULT FALSE,
  after_hours_sms_body TEXT,
  recording_enabled BOOLEAN DEFAULT FALSE,
  transcription_enabled BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS call_event (
  id SERIAL PRIMARY KEY,
  call_log_id INTEGER NOT NULL REFERENCES twilio_call_log(id) ON DELETE CASCADE,
  event_type VARCHAR(80) NOT NULL,
  provider_event_id VARCHAR(160),
  payload JSONB,
  created_at TIMESTAMP DEFAULT NOW(),
  CONSTRAINT uq_call_event_idempotency UNIQUE (call_log_id, event_type, provider_event_id)
);

CREATE INDEX IF NOT EXISTS ix_twilio_call_log_company_created ON twilio_call_log(company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_twilio_call_log_company_status ON twilio_call_log(company_id, status);
CREATE INDEX IF NOT EXISTS ix_twilio_call_log_twilio_sid ON twilio_call_log(twilio_sid);
CREATE INDEX IF NOT EXISTS ix_twilio_call_log_recording_sid ON twilio_call_log(recording_sid);
CREATE INDEX IF NOT EXISTS ix_twilio_call_log_voicemail_sid ON twilio_call_log(voicemail_sid);
CREATE INDEX IF NOT EXISTS ix_phone_settings_company ON phone_settings(company_id);
CREATE INDEX IF NOT EXISTS ix_call_event_call_log_id ON call_event(call_log_id);
CREATE INDEX IF NOT EXISTS ix_call_event_type_created ON call_event(event_type, created_at DESC);

COMMIT;
