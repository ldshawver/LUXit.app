-- Manual SMS send idempotency. Forward-only, idempotent.
--
-- A browser/network retry (double-tap, timeout-and-resubmit) of a manual
-- conversation send must never produce a second real Twilio send. This table
-- is the durable claim a request takes on one (company, idempotency_key)
-- pair before calling Twilio; a concurrent/retried request for the same key
-- reads the first request's outcome instead of sending again. Distinct from
-- the existing sms_outbound_intent/sms_outbound_attempt tables, which model
-- an inbound-triggered automated reply effect, not a human-initiated send.
--
-- Additive only. No existing table/column changes.

CREATE TABLE IF NOT EXISTS sms_send_idempotency (
    id              SERIAL PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES company(id),
    user_id         INTEGER REFERENCES "user"(id),
    conversation_id INTEGER NOT NULL REFERENCES twilio_conversation(id),
    idempotency_key VARCHAR(128) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'sending',
    twilio_sid      VARCHAR(100),
    error_message   TEXT,
    created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP WITHOUT TIME ZONE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sms_send_idempotency_key
    ON sms_send_idempotency (company_id, idempotency_key);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sms_send_idempotency_twilio_sid
    ON sms_send_idempotency (twilio_sid)
    WHERE twilio_sid IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_sms_send_idempotency_company_id ON sms_send_idempotency (company_id);
CREATE INDEX IF NOT EXISTS ix_sms_send_idempotency_user_id ON sms_send_idempotency (user_id);
CREATE INDEX IF NOT EXISTS ix_sms_send_idempotency_conversation_id ON sms_send_idempotency (conversation_id);
CREATE INDEX IF NOT EXISTS ix_sms_send_idempotency_status ON sms_send_idempotency (status);
