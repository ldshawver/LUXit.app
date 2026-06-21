-- LUXit tenant license, billing, and feature management
-- Idempotent forward migration only. Do not create rollback migrations for normal deploys.

CREATE TABLE IF NOT EXISTS feature_module (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) NOT NULL UNIQUE,
    name VARCHAR(160) NOT NULL,
    description TEXT,
    category VARCHAR(80),
    is_active BOOLEAN DEFAULT TRUE,
    default_monthly_price NUMERIC(10,2) DEFAULT 0,
    stripe_product_id VARCHAR(120),
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_license (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    feature_key VARCHAR(100) NOT NULL,
    status VARCHAR(30) DEFAULT 'active',
    seats_included INTEGER DEFAULT 1,
    seats_used INTEGER DEFAULT 0,
    monthly_price NUMERIC(10,2) DEFAULT 0,
    billing_cycle VARCHAR(20) DEFAULT 'monthly',
    starts_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    trial_ends_at TIMESTAMP WITHOUT TIME ZONE,
    renews_at TIMESTAMP WITHOUT TIME ZONE,
    canceled_at TIMESTAMP WITHOUT TIME ZONE,
    suspended_at TIMESTAMP WITHOUT TIME ZONE,
    suspension_reason TEXT,
    auto_disable_enabled BOOLEAN DEFAULT TRUE,
    grace_period_days INTEGER DEFAULT 7,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    UNIQUE(company_id, feature_key)
);

CREATE TABLE IF NOT EXISTS tenant_billing_account (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL UNIQUE REFERENCES company(id),
    stripe_customer_id VARCHAR(120),
    default_payment_method_id VARCHAR(120),
    billing_email VARCHAR(255),
    billing_contact_name VARCHAR(255),
    billing_address_json JSONB,
    autopay_enabled BOOLEAN DEFAULT FALSE,
    payment_status VARCHAR(50) DEFAULT 'none',
    last_payment_at TIMESTAMP WITHOUT TIME ZONE,
    last_payment_failed_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_subscription (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    stripe_subscription_id VARCHAR(120) UNIQUE,
    status VARCHAR(50),
    current_period_start TIMESTAMP WITHOUT TIME ZONE,
    current_period_end TIMESTAMP WITHOUT TIME ZONE,
    cancel_at_period_end BOOLEAN DEFAULT FALSE,
    amount_due INTEGER DEFAULT 0,
    currency VARCHAR(10) DEFAULT 'usd',
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tenant_invoice (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    stripe_invoice_id VARCHAR(120) UNIQUE,
    invoice_number VARCHAR(120),
    status VARCHAR(50),
    amount_due INTEGER DEFAULT 0,
    amount_paid INTEGER DEFAULT 0,
    currency VARCHAR(10) DEFAULT 'usd',
    hosted_invoice_url TEXT,
    invoice_pdf TEXT,
    due_date TIMESTAMP WITHOUT TIME ZONE,
    paid_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS license_event_log (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id),
    license_id INTEGER REFERENCES tenant_license(id),
    actor_user_id INTEGER REFERENCES "user"(id),
    actor_role VARCHAR(50),
    event_type VARCHAR(80) NOT NULL,
    old_status VARCHAR(30),
    new_status VARCHAR(30),
    details_json JSONB,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS billing_email_template (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES company(id),
    name VARCHAR(160) NOT NULL,
    subject VARCHAR(255),
    body_html TEXT,
    body_text TEXT,
    event_type VARCHAR(80),
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS billing_automation_rule (
    id SERIAL PRIMARY KEY,
    company_id INTEGER REFERENCES company(id),
    scope VARCHAR(20) DEFAULT 'global',
    event_type VARCHAR(80) NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    delay_days INTEGER DEFAULT 0,
    email_template_id INTEGER REFERENCES billing_email_template(id),
    action VARCHAR(80) NOT NULL,
    created_by_user_id INTEGER REFERENCES "user"(id),
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

-- Safe additive guards for environments where an earlier partial migration created tables.
ALTER TABLE tenant_license ADD COLUMN IF NOT EXISTS auto_disable_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE tenant_license ADD COLUMN IF NOT EXISTS grace_period_days INTEGER DEFAULT 7;
ALTER TABLE tenant_license ADD COLUMN IF NOT EXISTS suspension_reason TEXT;
ALTER TABLE tenant_billing_account ADD COLUMN IF NOT EXISTS autopay_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE tenant_invoice ADD COLUMN IF NOT EXISTS hosted_invoice_url TEXT;
ALTER TABLE tenant_invoice ADD COLUMN IF NOT EXISTS invoice_pdf TEXT;

CREATE INDEX IF NOT EXISTS ix_feature_module_key ON feature_module(key);
CREATE INDEX IF NOT EXISTS ix_tenant_license_company_feature ON tenant_license(company_id, feature_key);
CREATE INDEX IF NOT EXISTS ix_tenant_license_status ON tenant_license(status);
CREATE INDEX IF NOT EXISTS ix_tenant_billing_account_company ON tenant_billing_account(company_id);
CREATE INDEX IF NOT EXISTS ix_tenant_invoice_company_status ON tenant_invoice(company_id, status);
CREATE INDEX IF NOT EXISTS ix_tenant_invoice_stripe ON tenant_invoice(stripe_invoice_id);
CREATE INDEX IF NOT EXISTS ix_license_event_log_company_license ON license_event_log(company_id, license_id);
CREATE INDEX IF NOT EXISTS ix_billing_automation_rule_event ON billing_automation_rule(event_type, enabled);

INSERT INTO feature_module (key, name, description, category, default_monthly_price, is_active)
VALUES
('phone_pwa_communications', 'Phone/PWA Communications', 'Standalone phone, SMS inbox, calls, voicemail, push alerts, and PWA communications.', 'communications', 99.00, TRUE),
('sms_campaigns', 'SMS Campaigns', 'Scheduled and batched SMS/MMS campaign sending.', 'marketing', 49.00, TRUE),
('crm_contacts', 'CRM Contacts', 'Unified CRM contacts, imports, consent, and segmentation.', 'crm', 29.00, TRUE),
('ai_agents', 'AI Agents', 'AI assistants, automations, and recommendations.', 'ai', 79.00, TRUE),
('marketing_calendar', 'Marketing Calendar', 'Campaign planning calendar and scheduling.', 'marketing', 19.00, TRUE),
('analytics_reports', 'Analytics & Reports', 'Marketing, campaign, and operational reporting.', 'analytics', 39.00, TRUE),
('pos_myorder', 'POS / MyOrder', 'MyOrder.fun point-of-sale and ordering module.', 'pos', 99.00, TRUE),
('document_hub', 'Document Hub', 'Documents, templates, and file workflows.', 'operations', 29.00, TRUE),
('contractor_hub', 'Contractor Hub', 'Contractor/vendor management workflows.', 'operations', 39.00, TRUE)
ON CONFLICT (key) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    category = EXCLUDED.category,
    default_monthly_price = EXCLUDED.default_monthly_price,
    is_active = TRUE,
    updated_at = NOW();

INSERT INTO tenant_license (company_id, feature_key, status, seats_included, seats_used, monthly_price, billing_cycle, starts_at, auto_disable_enabled, grace_period_days)
SELECT 1, 'phone_pwa_communications', 'active', 999, 0, 0, 'monthly', NOW(), TRUE, 7
WHERE EXISTS (SELECT 1 FROM company WHERE id = 1)
ON CONFLICT (company_id, feature_key) DO NOTHING;
