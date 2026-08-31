-- Promotional opt-in workflow — forward-only, idempotent.
--
-- Two additive tables. Neither changes existing contact/consent columns, the
-- campaign resolver, or the STOP/START keyword contract. A promotional
-- solicitation in status 'pending' is the *context* a later inbound YES needs
-- to become promotional consent; the consent event is the immutable proof and
-- is idempotent on the inbound Twilio MessageSid.

CREATE TABLE IF NOT EXISTS promotional_optin_solicitation (
    id                        SERIAL PRIMARY KEY,
    company_id                INTEGER NOT NULL REFERENCES company(id),
    contact_id                INTEGER NOT NULL REFERENCES contact(id),
    canonical_phone           VARCHAR(32) NOT NULL,
    business_phone_number     VARCHAR(32),
    status                    VARCHAR(20) NOT NULL DEFAULT 'pending',
    solicitation_body         TEXT,
    solicitation_message_sid  VARCHAR(64),
    solicited_at              TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    solicited_by_user_id      INTEGER REFERENCES "user"(id),
    consent_message_sid       VARCHAR(64),
    consented_at              TIMESTAMP WITHOUT TIME ZONE,
    closed_at                 TIMESTAMP WITHOUT TIME ZONE,
    closed_reason             VARCHAR(60),
    source                    VARCHAR(40) NOT NULL DEFAULT 'operator',
    created_at                TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at                TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_promo_solicitation_company        ON promotional_optin_solicitation (company_id);
CREATE INDEX IF NOT EXISTS ix_promo_solicitation_contact        ON promotional_optin_solicitation (contact_id);
CREATE INDEX IF NOT EXISTS ix_promo_solicitation_canonical_phone ON promotional_optin_solicitation (canonical_phone);
CREATE INDEX IF NOT EXISTS ix_promo_solicitation_business_phone  ON promotional_optin_solicitation (business_phone_number);
CREATE INDEX IF NOT EXISTS ix_promo_solicitation_status          ON promotional_optin_solicitation (status);

-- At most one pending solicitation per (tenant, contact): makes create_solicitation idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_solicitation_one_pending
    ON promotional_optin_solicitation (company_id, contact_id)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS promotional_consent_event (
    id                        SERIAL PRIMARY KEY,
    company_id                INTEGER NOT NULL REFERENCES company(id),
    contact_id                INTEGER NOT NULL REFERENCES contact(id),
    solicitation_id           INTEGER REFERENCES promotional_optin_solicitation(id),
    canonical_phone           VARCHAR(32) NOT NULL,
    business_phone_number     VARCHAR(32),
    inbound_message_sid       VARCHAR(64) NOT NULL,
    solicitation_message_sid  VARCHAR(64),
    solicited_at              TIMESTAMP WITHOUT TIME ZONE,
    consent_purpose           VARCHAR(20) NOT NULL DEFAULT 'promotional',
    consent_source            VARCHAR(40) NOT NULL DEFAULT 'sms_reply_yes',
    consent_keyword           VARCHAR(20),
    consented_at              TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    created_at                TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS ix_promo_consent_event_company     ON promotional_consent_event (company_id);
CREATE INDEX IF NOT EXISTS ix_promo_consent_event_contact     ON promotional_consent_event (contact_id);
CREATE INDEX IF NOT EXISTS ix_promo_consent_event_solicitation ON promotional_consent_event (solicitation_id);

-- Idempotency: one consent event per inbound webhook MessageSid.
CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_consent_event_inbound_sid
    ON promotional_consent_event (inbound_message_sid);
