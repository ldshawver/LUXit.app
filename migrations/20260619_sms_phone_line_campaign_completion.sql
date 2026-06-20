-- Per-number phone-line settings and scheduled SMS campaign completion fields.

ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS status_callback_webhook_url VARCHAR(500);
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS number_auto_reply_text TEXT;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS campaign_sender_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS campaign_default_batch_size INTEGER DEFAULT 50;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS campaign_send_rate_per_minute INTEGER DEFAULT 60;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS allow_global_fallback BOOLEAN DEFAULT FALSE;

ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS media_urls JSONB DEFAULT '[]'::jsonb;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS batch_size INTEGER DEFAULT 50;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS send_rate_per_minute INTEGER DEFAULT 60;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS canceled_at TIMESTAMP;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_twilio_phone_number_company_campaign_sender
    ON twilio_phone_number(company_id, campaign_sender_enabled, is_active, sms_enabled);
CREATE INDEX IF NOT EXISTS ix_sms_campaign_company_status_scheduled
    ON sms_campaign(company_id, status, scheduled_at);
