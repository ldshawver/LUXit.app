-- Idempotent SMS campaign compliance additions for PostgreSQL.
ALTER TABLE contact ADD COLUMN IF NOT EXISTS sms_marketing_opt_in BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS sms_marketing_opt_in_at TIMESTAMP NULL;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS sms_marketing_opt_in_source VARCHAR(120) NULL;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS sms_opt_out_at TIMESTAMP NULL;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS sms_consent_status VARCHAR(30) NOT NULL DEFAULT 'unknown';
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES company(id);
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS audience_filter JSONB NULL DEFAULT '{}'::jsonb;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS estimated_recipient_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS test_sent_at TIMESTAMP NULL;
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS phone_number VARCHAR(50) NULL;
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS message_sid VARCHAR(100) NULL;
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS provider_message_sid VARCHAR(255) NULL;
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS error_code VARCHAR(50) NULL;
ALTER TABLE twilio_account ADD COLUMN IF NOT EXISTS after_hours_cooldown_minutes INTEGER NOT NULL DEFAULT 720;
CREATE INDEX IF NOT EXISTS ix_sms_campaign_company_id ON sms_campaign(company_id);
CREATE INDEX IF NOT EXISTS ix_sms_recipient_campaign_status ON sms_recipient(campaign_id, status);
CREATE INDEX IF NOT EXISTS ix_sms_recipient_provider_message_sid ON sms_recipient(provider_message_sid);
