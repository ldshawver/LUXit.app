-- Marketing Hub SMS/Social durable schema migration
-- Safe to run multiple times on PostgreSQL.

ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS company_id INTEGER;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS objective TEXT;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS segment VARCHAR(100);
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'draft';
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_sms_campaign_company_id ON sms_campaign(company_id);
CREATE INDEX IF NOT EXISTS ix_sms_campaign_company_status ON sms_campaign(company_id, status);

ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS company_id INTEGER;
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS campaign_id INTEGER;
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS contact_id INTEGER;
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS status VARCHAR(50);
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS provider_message_sid VARCHAR(120);
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS replied_at TIMESTAMP;
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS opted_out_at TIMESTAMP;
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS provider_error_code VARCHAR(50);
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE sms_recipient ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_sms_recipient_company_id ON sms_recipient(company_id);
CREATE INDEX IF NOT EXISTS ix_sms_recipient_campaign_id ON sms_recipient(campaign_id);
CREATE INDEX IF NOT EXISTS ix_sms_recipient_contact_id ON sms_recipient(contact_id);
CREATE INDEX IF NOT EXISTS ix_sms_recipient_provider_message_sid ON sms_recipient(provider_message_sid);

ALTER TABLE social_post ADD COLUMN IF NOT EXISTS company_id INTEGER;
ALTER TABLE social_post ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE social_post ADD COLUMN IF NOT EXISTS platforms JSONB;
ALTER TABLE social_post ADD COLUMN IF NOT EXISTS media_urls JSONB;
ALTER TABLE social_post ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_social_post_company_id ON social_post(company_id);

CREATE TABLE IF NOT EXISTS sms_keyword_rule (
    id SERIAL PRIMARY KEY,
    company_id INTEGER,
    campaign_id INTEGER,
    keyword VARCHAR(80) NOT NULL,
    match_type VARCHAR(30) DEFAULT 'exact',
    reply_message TEXT,
    priority INTEGER DEFAULT 100,
    is_active BOOLEAN DEFAULT TRUE,
    business_hours_only BOOLEAN DEFAULT FALSE,
    after_hours_message TEXT,
    tag_to_add VARCHAR(100),
    segment_to_add VARCHAR(100),
    notify_admin BOOLEAN DEFAULT FALSE,
    created_by_user_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_sms_keyword_rule_company_id ON sms_keyword_rule(company_id);
CREATE INDEX IF NOT EXISTS ix_sms_keyword_rule_campaign_id ON sms_keyword_rule(campaign_id);
CREATE INDEX IF NOT EXISTS ix_sms_keyword_rule_priority ON sms_keyword_rule(company_id, is_active, priority);

CREATE TABLE IF NOT EXISTS sms_auto_reply_rule (
    id SERIAL PRIMARY KEY,
    company_id INTEGER,
    campaign_id INTEGER,
    name VARCHAR(255) NOT NULL,
    trigger_type VARCHAR(50) DEFAULT 'inbound',
    reply_message TEXT,
    after_hours_message TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_by_user_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_sms_auto_reply_rule_company_id ON sms_auto_reply_rule(company_id);
CREATE INDEX IF NOT EXISTS ix_sms_auto_reply_rule_campaign_id ON sms_auto_reply_rule(campaign_id);

CREATE TABLE IF NOT EXISTS marketing_audit_log (
    id SERIAL PRIMARY KEY,
    company_id INTEGER,
    created_by_user_id INTEGER,
    entity_type VARCHAR(80),
    entity_id INTEGER,
    action VARCHAR(80) NOT NULL,
    details JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_marketing_audit_log_company_id ON marketing_audit_log(company_id);
CREATE INDEX IF NOT EXISTS ix_marketing_audit_log_entity ON marketing_audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_contact_company_phone ON contact(company_id, phone);
CREATE INDEX IF NOT EXISTS ix_twilio_conversation_company_from ON twilio_conversation(company_id, from_number);
CREATE INDEX IF NOT EXISTS ix_twilio_phone_number_company_phone ON twilio_phone_number(company_id, phone_number);

-- Idempotency and integrity constraints. Foreign keys/checks are NOT VALID so
-- production databases with older backfilled data can adopt them safely, then
-- validate after cleanup if needed.
CREATE UNIQUE INDEX IF NOT EXISTS ux_sms_recipient_provider_message_sid_not_null
    ON sms_recipient(provider_message_sid) WHERE provider_message_sid IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_sms_recipient_campaign_contact
    ON sms_recipient(campaign_id, contact_id) WHERE campaign_id IS NOT NULL AND contact_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sms_campaign_company') THEN
        ALTER TABLE sms_campaign ADD CONSTRAINT fk_sms_campaign_company FOREIGN KEY (company_id) REFERENCES company(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sms_campaign_created_by') THEN
        ALTER TABLE sms_campaign ADD CONSTRAINT fk_sms_campaign_created_by FOREIGN KEY (created_by_user_id) REFERENCES "user"(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_sms_campaign_status') THEN
        ALTER TABLE sms_campaign ADD CONSTRAINT ck_sms_campaign_status CHECK (status IS NULL OR status IN ('draft','scheduled','sending','sent','paused','canceled','failed','archived')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sms_recipient_company') THEN
        ALTER TABLE sms_recipient ADD CONSTRAINT fk_sms_recipient_company FOREIGN KEY (company_id) REFERENCES company(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sms_recipient_campaign') THEN
        ALTER TABLE sms_recipient ADD CONSTRAINT fk_sms_recipient_campaign FOREIGN KEY (campaign_id) REFERENCES sms_campaign(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sms_recipient_contact') THEN
        ALTER TABLE sms_recipient ADD CONSTRAINT fk_sms_recipient_contact FOREIGN KEY (contact_id) REFERENCES contact(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_sms_recipient_status') THEN
        ALTER TABLE sms_recipient ADD CONSTRAINT ck_sms_recipient_status CHECK (status IS NULL OR status IN ('queued','draft','pending','sending','sent','delivered','failed','undelivered','replied','opted_out','canceled')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_social_post_company') THEN
        ALTER TABLE social_post ADD CONSTRAINT fk_social_post_company FOREIGN KEY (company_id) REFERENCES company(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_social_post_user') THEN
        ALTER TABLE social_post ADD CONSTRAINT fk_social_post_user FOREIGN KEY (user_id) REFERENCES "user"(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sms_keyword_rule_company') THEN
        ALTER TABLE sms_keyword_rule ADD CONSTRAINT fk_sms_keyword_rule_company FOREIGN KEY (company_id) REFERENCES company(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sms_keyword_rule_campaign') THEN
        ALTER TABLE sms_keyword_rule ADD CONSTRAINT fk_sms_keyword_rule_campaign FOREIGN KEY (campaign_id) REFERENCES sms_campaign(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_sms_keyword_rule_match_type') THEN
        ALTER TABLE sms_keyword_rule ADD CONSTRAINT ck_sms_keyword_rule_match_type CHECK (match_type IN ('exact','contains','starts_with','starts-with','prefix')) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sms_auto_reply_rule_company') THEN
        ALTER TABLE sms_auto_reply_rule ADD CONSTRAINT fk_sms_auto_reply_rule_company FOREIGN KEY (company_id) REFERENCES company(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_sms_auto_reply_rule_campaign') THEN
        ALTER TABLE sms_auto_reply_rule ADD CONSTRAINT fk_sms_auto_reply_rule_campaign FOREIGN KEY (campaign_id) REFERENCES sms_campaign(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_marketing_audit_log_company') THEN
        ALTER TABLE marketing_audit_log ADD CONSTRAINT fk_marketing_audit_log_company FOREIGN KEY (company_id) REFERENCES company(id) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_marketing_audit_log_created_by') THEN
        ALTER TABLE marketing_audit_log ADD CONSTRAINT fk_marketing_audit_log_created_by FOREIGN KEY (created_by_user_id) REFERENCES "user"(id) NOT VALID;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION prevent_marketing_audit_log_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'marketing_audit_log is append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_marketing_audit_log_append_only ON marketing_audit_log;
CREATE TRIGGER trg_marketing_audit_log_append_only
    BEFORE UPDATE OR DELETE ON marketing_audit_log
    FOR EACH ROW EXECUTE FUNCTION prevent_marketing_audit_log_mutation();

