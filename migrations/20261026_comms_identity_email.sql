-- Unified communications (differentiators M4): business identities, their
-- phone numbers, and 1:1 email history. Forward-only, idempotent, additive.

CREATE TABLE IF NOT EXISTS business_identity (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    email_from_name VARCHAR(120),
    email_from_address VARCHAR(255),
    signature TEXT,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_business_identity_company_id ON business_identity (company_id);

CREATE TABLE IF NOT EXISTS business_identity_number (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    identity_id INTEGER NOT NULL REFERENCES business_identity(id) ON DELETE CASCADE,
    phone_number_id INTEGER NOT NULL UNIQUE REFERENCES twilio_phone_number(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_business_identity_number_company_id ON business_identity_number (company_id);
CREATE INDEX IF NOT EXISTS ix_business_identity_number_identity_id ON business_identity_number (identity_id);

CREATE TABLE IF NOT EXISTS email_message (
    id SERIAL PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    contact_id INTEGER REFERENCES contact(id) ON DELETE SET NULL,
    identity_id INTEGER REFERENCES business_identity(id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL,
    direction VARCHAR(10) NOT NULL,
    mailbox VARCHAR(255),
    from_address VARCHAR(255),
    to_addresses JSON,
    cc_addresses JSON,
    subject VARCHAR(500),
    body_text TEXT,
    thread_key VARCHAR(255),
    provider VARCHAR(40),
    provider_message_id VARCHAR(255),
    status VARCHAR(30) NOT NULL DEFAULT 'received',
    occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    CONSTRAINT uq_email_message_provider_id UNIQUE (company_id, provider_message_id)
);
CREATE INDEX IF NOT EXISTS ix_email_message_company_id ON email_message (company_id);
CREATE INDEX IF NOT EXISTS ix_email_message_contact_id ON email_message (contact_id);
CREATE INDEX IF NOT EXISTS ix_email_message_thread_key ON email_message (thread_key);
CREATE INDEX IF NOT EXISTS ix_email_message_occurred_at ON email_message (occurred_at);
