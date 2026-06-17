-- Durable/idempotent SMS Campaign audit schema repair.
-- Safe for PostgreSQL production: only adds missing columns/indexes and preserves rows.

ALTER TABLE sms_campaign
    ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES company(id),
    ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER NULL REFERENCES "user"(id),
    ADD COLUMN IF NOT EXISTS name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS objective TEXT,
    ADD COLUMN IF NOT EXISTS message VARCHAR(1000),
    ADD COLUMN IF NOT EXISTS segment VARCHAR(100),
    ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'draft',
    ADD COLUMN IF NOT EXISTS audience_filter JSONB DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS estimated_recipient_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS test_sent_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS sent_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now();

ALTER TABLE sms_recipient
    ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES company(id),
    ADD COLUMN IF NOT EXISTS campaign_id INTEGER NULL REFERENCES sms_campaign(id),
    ADD COLUMN IF NOT EXISTS contact_id INTEGER NULL REFERENCES contact(id),
    ADD COLUMN IF NOT EXISTS phone_number VARCHAR(50),
    ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS message_sid VARCHAR(100),
    ADD COLUMN IF NOT EXISTS provider_message_sid VARCHAR(255),
    ADD COLUMN IF NOT EXISTS error_code VARCHAR(50),
    ADD COLUMN IF NOT EXISTS sent_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS replied_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS opted_out_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS provider_error_code VARCHAR(50),
    ADD COLUMN IF NOT EXISTS error_message TEXT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now();

ALTER TABLE sms_template
    ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES company(id),
    ADD COLUMN IF NOT EXISTS name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS message TEXT,
    ADD COLUMN IF NOT EXISTS category VARCHAR(100),
    ADD COLUMN IF NOT EXISTS tone VARCHAR(50),
    ADD COLUMN IF NOT EXISTS has_opt_out BOOLEAN DEFAULT true,
    ADD COLUMN IF NOT EXISTS is_compliant BOOLEAN DEFAULT true,
    ADD COLUMN IF NOT EXISTS usage_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT true,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now();

ALTER TABLE sms_keyword_rule
    ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES company(id),
    ADD COLUMN IF NOT EXISTS campaign_id INTEGER NULL REFERENCES sms_campaign(id),
    ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER NULL REFERENCES "user"(id),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now();

ALTER TABLE sms_auto_reply_rule
    ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES company(id),
    ADD COLUMN IF NOT EXISTS campaign_id INTEGER NULL REFERENCES sms_campaign(id),
    ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER NULL REFERENCES "user"(id),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now();

ALTER TABLE segment ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES company(id);

ALTER TABLE calendar_event
    ADD COLUMN IF NOT EXISTS company_id INTEGER NULL REFERENCES company(id),
    ADD COLUMN IF NOT EXISTS created_by_id INTEGER NULL REFERENCES "user"(id),
    ADD COLUMN IF NOT EXISTS title VARCHAR(255),
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS channel VARCHAR(50),
    ADD COLUMN IF NOT EXISTS content_type VARCHAR(80),
    ADD COLUMN IF NOT EXISTS content_id INTEGER,
    ADD COLUMN IF NOT EXISTS status VARCHAR(50),
    ADD COLUMN IF NOT EXISTS audience VARCHAR(255),
    ADD COLUMN IF NOT EXISTS estimated_recipient_count INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS end_date TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS all_day BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS notes TEXT,
    ADD COLUMN IF NOT EXISTS color VARCHAR(40),
    ADD COLUMN IF NOT EXISTS deadline_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT now();

CREATE INDEX IF NOT EXISTS ix_sms_campaign_company_status ON sms_campaign(company_id, status);
CREATE INDEX IF NOT EXISTS ix_sms_campaign_company_scheduled_at ON sms_campaign(company_id, scheduled_at);
CREATE INDEX IF NOT EXISTS ix_sms_recipient_campaign_status ON sms_recipient(campaign_id, status);
CREATE INDEX IF NOT EXISTS ix_sms_recipient_company_id ON sms_recipient(company_id);
CREATE INDEX IF NOT EXISTS ix_sms_recipient_provider_message_sid ON sms_recipient(provider_message_sid);
CREATE INDEX IF NOT EXISTS ix_sms_template_company_active ON sms_template(company_id, is_active);
CREATE INDEX IF NOT EXISTS ix_sms_keyword_rule_company_id ON sms_keyword_rule(company_id);
CREATE INDEX IF NOT EXISTS ix_sms_auto_reply_rule_company_id ON sms_auto_reply_rule(company_id);
CREATE INDEX IF NOT EXISTS ix_segment_company_id ON segment(company_id);
CREATE INDEX IF NOT EXISTS ix_calendar_event_company_channel_start ON calendar_event(company_id, channel, start_date);
CREATE INDEX IF NOT EXISTS ix_calendar_event_sms_content ON calendar_event(content_type, content_id) WHERE content_type = 'sms_campaign';
