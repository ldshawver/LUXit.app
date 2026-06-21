-- PWA communications completion: alert preferences and per-number voicemail greetings.
-- Safe to run repeatedly; additive only and preserves existing data.

ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_text_alerts_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_call_alerts_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_voicemail_alerts_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_unread_reminder_alerts_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_vibration_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_alerts_business_hours_only BOOLEAN DEFAULT TRUE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_quiet_hours_start VARCHAR(5);
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_quiet_hours_end VARCHAR(5);
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_unread_repeat_minutes INTEGER DEFAULT 1;

CREATE TABLE IF NOT EXISTS voice_greeting (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    phone_number_id INTEGER NOT NULL REFERENCES twilio_phone_number(id),
    name VARCHAR(160) NOT NULL,
    greeting_type VARCHAR(30) NOT NULL DEFAULT 'standard',
    text_body TEXT,
    audio_url VARCHAR(500),
    storage_path VARCHAR(500),
    voice_name VARCHAR(120),
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    applies_to VARCHAR(40) NOT NULL DEFAULT 'voicemail_default',
    created_by_user_id INTEGER REFERENCES "user"(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_voice_greeting_company_phone ON voice_greeting(company_id, phone_number_id);
CREATE INDEX IF NOT EXISTS ix_voice_greeting_active_scope ON voice_greeting(phone_number_id, applies_to, is_active);
CREATE INDEX IF NOT EXISTS ix_notification_unread_reminder_dedupe ON notification(company_id, phone_number_id, event_type, created_at);
