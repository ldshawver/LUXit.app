-- Hosted promotional web opt-in (customer_web_optin). Forward-only, idempotent.
--
-- A second consent-granting channel alongside the contextual inbound SMS YES.
-- A signed per-contact link renders a hosted MyOrder.fun "Text Specials"
-- consent page; an affirmative submission grants promotional consent through
-- the SAME service path as an inbound YES and records immutable evidence
-- (exact disclosure text + version + request context).
--
-- Additive only. Nothing here changes the campaign resolver, the STOP/START
-- keyword contract, the operator send flow, or the existing contextual-YES
-- idempotency. The one non-additive change is dropping NOT NULL on
-- promotional_consent_event.inbound_message_sid so a web event (which has no
-- inbound MessageSid) can be recorded; the column stays UNIQUE and Postgres
-- allows many NULLs under a UNIQUE constraint.

-- promotional_optin_solicitation: the hosted-link channel on the existing
-- "one consent context per contact" row. source='web_optin' for a link-first
-- row; web_token_jti is the id embedded in the signed link.
ALTER TABLE promotional_optin_solicitation ADD COLUMN IF NOT EXISTS web_token_jti               VARCHAR(64);
ALTER TABLE promotional_optin_solicitation ADD COLUMN IF NOT EXISTS web_link_disclosure_version VARCHAR(40);
ALTER TABLE promotional_optin_solicitation ADD COLUMN IF NOT EXISTS web_link_created_at         TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE promotional_optin_solicitation ADD COLUMN IF NOT EXISTS web_link_created_by_user_id INTEGER;
ALTER TABLE promotional_optin_solicitation ADD COLUMN IF NOT EXISTS web_consent_at              TIMESTAMP WITHOUT TIME ZONE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_solicitation_web_token_jti
    ON promotional_optin_solicitation (web_token_jti)
    WHERE web_token_jti IS NOT NULL;

-- promotional_consent_event: web-channel idempotency key + immutable evidence.
ALTER TABLE promotional_consent_event ALTER COLUMN inbound_message_sid DROP NOT NULL;
ALTER TABLE promotional_consent_event ADD COLUMN IF NOT EXISTS web_token_jti        VARCHAR(64);
ALTER TABLE promotional_consent_event ADD COLUMN IF NOT EXISTS disclosure_version   VARCHAR(40);
ALTER TABLE promotional_consent_event ADD COLUMN IF NOT EXISTS disclosure_text      TEXT;
ALTER TABLE promotional_consent_event ADD COLUMN IF NOT EXISTS consent_context      JSONB;

CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_consent_event_web_token_jti
    ON promotional_consent_event (web_token_jti)
    WHERE web_token_jti IS NOT NULL;
