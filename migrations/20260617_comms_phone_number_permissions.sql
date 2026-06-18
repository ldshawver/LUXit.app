-- Communications phone-number permissions and per-line settings
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS manage_users_enabled BOOLEAN DEFAULT FALSE;

ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS business_hours JSONB DEFAULT '{}'::jsonb;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS timezone VARCHAR(80) DEFAULT 'America/Los_Angeles';
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS during_hours_route VARCHAR(30) DEFAULT 'ring_pwa';
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS after_hours_route VARCHAR(30) DEFAULT 'voicemail';
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS browser_calling_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS cell_callback_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS wifi_only BOOLEAN DEFAULT FALSE;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS mobile_data_allowed BOOLEAN DEFAULT TRUE;
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS fallback_behavior VARCHAR(30) DEFAULT 'cell_callback';
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS caller_id_display_name VARCHAR(120);

CREATE TABLE IF NOT EXISTS phone_number_user_permission (
  id SERIAL PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES company(id),
  phone_number_id INTEGER NOT NULL REFERENCES twilio_phone_number(id),
  user_id INTEGER NOT NULL REFERENCES "user"(id),
  can_access_pwa BOOLEAN DEFAULT TRUE,
  can_view_sms BOOLEAN DEFAULT TRUE,
  can_send_sms BOOLEAN DEFAULT TRUE,
  can_view_calls BOOLEAN DEFAULT TRUE,
  can_call BOOLEAN DEFAULT TRUE,
  can_view_voicemail BOOLEAN DEFAULT TRUE,
  can_manage_number BOOLEAN DEFAULT FALSE,
  can_send_campaigns BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_phone_number_user_permission UNIQUE (phone_number_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_phone_number_user_permission_company_id ON phone_number_user_permission(company_id);
CREATE INDEX IF NOT EXISTS ix_phone_number_user_permission_phone_number_id ON phone_number_user_permission(phone_number_id);
CREATE INDEX IF NOT EXISTS ix_phone_number_user_permission_user_id ON phone_number_user_permission(user_id);


ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS from_phone_number_id INTEGER REFERENCES twilio_phone_number(id);
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS from_phone_number VARCHAR(20);
CREATE INDEX IF NOT EXISTS ix_sms_campaign_from_phone_number_id ON sms_campaign(from_phone_number_id);

ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_palette_id VARCHAR(30) DEFAULT 'lux';
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_theme_mode VARCHAR(20) DEFAULT 'dark';
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_preferences_updated_at TIMESTAMP;

ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS read_at TIMESTAMP;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS read_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS callback_target VARCHAR(20);
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS transcription_error TEXT;
ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS transcribed_at TIMESTAMP;

ALTER TABLE voice_voicemail_message ADD COLUMN IF NOT EXISTS transcription_text TEXT;
ALTER TABLE voice_voicemail_message ADD COLUMN IF NOT EXISTS transcription_status VARCHAR(30) DEFAULT 'not_requested';
ALTER TABLE voice_voicemail_message ADD COLUMN IF NOT EXISTS transcription_provider VARCHAR(80);
ALTER TABLE voice_voicemail_message ADD COLUMN IF NOT EXISTS transcription_error TEXT;
ALTER TABLE voice_voicemail_message ADD COLUMN IF NOT EXISTS transcribed_at TIMESTAMP;
ALTER TABLE voice_voicemail_message ADD COLUMN IF NOT EXISTS read_at TIMESTAMP;
ALTER TABLE voice_voicemail_message ADD COLUMN IF NOT EXISTS read_by_user_id INTEGER REFERENCES "user"(id);

CREATE TABLE IF NOT EXISTS pwa_device (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    user_id INTEGER NOT NULL REFERENCES "user"(id),
    phone_number_id INTEGER REFERENCES twilio_phone_number(id),
    device_key VARCHAR(120) NOT NULL,
    device_name VARCHAR(120),
    browser VARCHAR(120),
    device_type VARCHAR(80),
    user_agent TEXT,
    online_status VARCHAR(20) DEFAULT 'online',
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    push_enabled BOOLEAN DEFAULT FALSE,
    microphone_permission VARCHAR(30) DEFAULT 'unknown',
    pwa_installed BOOLEAN DEFAULT FALSE,
    wifi_only BOOLEAN DEFAULT FALSE,
    cellular_callback_enabled BOOLEAN DEFAULT FALSE,
    mobile_data_calling_allowed BOOLEAN DEFAULT FALSE,
    default_calling_method VARCHAR(30) DEFAULT 'browser',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_pwa_device_user_key UNIQUE (company_id, user_id, device_key)
);
CREATE INDEX IF NOT EXISTS ix_pwa_device_company_id ON pwa_device(company_id);
CREATE INDEX IF NOT EXISTS ix_pwa_device_user_id ON pwa_device(user_id);
CREATE INDEX IF NOT EXISTS ix_pwa_device_phone_number_id ON pwa_device(phone_number_id);
