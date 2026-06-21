-- Additive compatibility guards for voice webhook notification/call-log fields.
-- Safe to run repeatedly; additive only and preserves data.

ALTER TABLE notification ADD COLUMN IF NOT EXISTS phone_number_id INTEGER REFERENCES twilio_phone_number(id);
ALTER TABLE notification ADD COLUMN IF NOT EXISTS event_type VARCHAR(50) DEFAULT 'system';
CREATE INDEX IF NOT EXISTS ix_notification_phone_number_id ON notification(phone_number_id);
CREATE INDEX IF NOT EXISTS ix_notification_company_event_type ON notification(company_id, event_type);

ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS phone_number_id INTEGER REFERENCES twilio_phone_number(id);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS parent_call_sid VARCHAR(100);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS forwarded_to_number VARCHAR(20);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS recording_url VARCHAR(500);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS recording_sid VARCHAR(100);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS voicemail_url VARCHAR(500);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS voicemail_sid VARCHAR(100);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS transcription_text TEXT;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS transcription_status VARCHAR(30) DEFAULT 'not_requested';
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS transcription_provider VARCHAR(80);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS transcription_error TEXT;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS transcribed_at TIMESTAMP;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS missed_text_sent BOOLEAN DEFAULT FALSE;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS raw_payload JSONB;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS metadata_json JSONB;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS read_at TIMESTAMP;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS read_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS callback_target VARCHAR(20);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS ix_twilio_call_log_phone_number_id ON twilio_call_log(phone_number_id);
CREATE INDEX IF NOT EXISTS ix_twilio_call_log_company_created ON twilio_call_log(company_id, created_at);

ALTER TABLE voice_voicemail_message ADD COLUMN IF NOT EXISTS phone_number_id INTEGER REFERENCES twilio_phone_number(id);
CREATE INDEX IF NOT EXISTS ix_voice_voicemail_message_phone_number_id ON voice_voicemail_message(phone_number_id);

-- Google/CRM contact cache fields used by PWA display-name backfill.
ALTER TABLE contact ADD COLUMN IF NOT EXISTS name VARCHAR(255);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS normalized_phone VARCHAR(32);
CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_phone ON contact(company_id, normalized_phone);
