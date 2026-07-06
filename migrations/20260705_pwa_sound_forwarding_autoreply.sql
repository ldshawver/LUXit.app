-- LUXit PWA sound diagnostics, per-line call forwarding, and business-hours SMS auto reply.
-- Safe additive migration for existing production databases.

ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS call_forwarding_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS call_forwarding_number VARCHAR(20);
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS business_hours_auto_reply_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS business_hours_auto_reply_text TEXT;

ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS forwarded BOOLEAN DEFAULT FALSE;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS forwarded_to VARCHAR(20);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS final_status VARCHAR(50);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS contact_id INTEGER;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS duration INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_twilio_phone_number_company_active
    ON twilio_phone_number (company_id, is_active);
CREATE INDEX IF NOT EXISTS ix_twilio_call_log_company_phone_status
    ON twilio_call_log (company_id, phone_number_id, status);
