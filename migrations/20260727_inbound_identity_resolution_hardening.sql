-- Forward-only, rerunnable hardening for inbound identity confirmation.
-- Historical values are retained in name_provenance; no contact/message or
-- consent history is deleted.

ALTER TABLE twilio_message
  ADD COLUMN IF NOT EXISTS processing_status VARCHAR(20) NOT NULL DEFAULT 'completed';
ALTER TABLE twilio_message
  ADD COLUMN IF NOT EXISTS processing_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE twilio_message
  ADD COLUMN IF NOT EXISTS processing_error_code VARCHAR(80);
ALTER TABLE twilio_message
  ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMP;
ALTER TABLE twilio_message
  ADD COLUMN IF NOT EXISTS processed_at TIMESTAMP;
ALTER TABLE twilio_message
  ADD COLUMN IF NOT EXISTS response_body TEXT;

CREATE INDEX IF NOT EXISTS ix_twilio_message_processing_status
  ON twilio_message(processing_status);

CREATE TABLE IF NOT EXISTS sms_outbound_intent (
  id SERIAL PRIMARY KEY,
  company_id INTEGER NOT NULL REFERENCES company(id),
  inbound_message_id INTEGER NOT NULL REFERENCES twilio_message(id),
  conversation_id INTEGER NOT NULL REFERENCES twilio_conversation(id),
  idempotency_key VARCHAR(64) NOT NULL UNIQUE,
  effect_type VARCHAR(50) NOT NULL,
  to_number VARCHAR(20),
  body TEXT NOT NULL,
  is_auto_reply BOOLEAN NOT NULL DEFAULT FALSE,
  rule_id INTEGER REFERENCES auto_reply_rule(id),
  status VARCHAR(30) NOT NULL DEFAULT 'pending',
  provider_sid VARCHAR(100) UNIQUE,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error_code VARCHAR(80),
  last_error_message TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW(),
  delivered_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_sms_outbound_intent_inbound
  ON sms_outbound_intent(inbound_message_id, status, id);
CREATE INDEX IF NOT EXISTS ix_sms_outbound_intent_company
  ON sms_outbound_intent(company_id);

CREATE TABLE IF NOT EXISTS sms_outbound_attempt (
  id SERIAL PRIMARY KEY,
  intent_id INTEGER NOT NULL REFERENCES sms_outbound_intent(id),
  attempt_number INTEGER NOT NULL,
  attempt_key VARCHAR(80) NOT NULL UNIQUE,
  status VARCHAR(30) NOT NULL,
  provider_sid VARCHAR(100) UNIQUE,
  provider_status VARCHAR(50),
  error_code VARCHAR(80),
  error_message TEXT,
  provider_response JSONB NOT NULL DEFAULT '{}'::jsonb,
  started_at TIMESTAMP NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMP,
  CONSTRAINT uq_sms_outbound_attempt_number UNIQUE(intent_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS ix_sms_outbound_attempt_intent
  ON sms_outbound_attempt(intent_id);

CREATE TABLE IF NOT EXISTS sms_recipient_delivery_attempt (
  id SERIAL PRIMARY KEY,
  company_id INTEGER REFERENCES company(id),
  campaign_id INTEGER REFERENCES sms_campaign(id),
  contact_id INTEGER REFERENCES contact(id),
  source_recipient_id INTEGER,
  provider_message_sid VARCHAR(255) NOT NULL UNIQUE,
  status VARCHAR(50),
  sent_at TIMESTAMP,
  delivered_at TIMESTAMP,
  replied_at TIMESTAMP,
  opted_out_at TIMESTAMP,
  error_code VARCHAR(50),
  provider_error_code VARCHAR(50),
  error_message TEXT,
  provider_response JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_sms_recipient_attempt_contact
  ON sms_recipient_delivery_attempt(contact_id);
CREATE INDEX IF NOT EXISTS ix_sms_recipient_attempt_campaign
  ON sms_recipient_delivery_attempt(campaign_id);
CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_phone_unmerged
  ON contact(company_id, normalized_phone, id)
  WHERE is_active IS TRUE AND merged_into_contact_id IS NULL
    AND normalized_phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_email_unmerged
  ON contact(company_id, normalized_email, id)
  WHERE is_active IS TRUE AND merged_into_contact_id IS NULL
    AND normalized_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contact_phone_company_norm_contact
  ON contact_phone_number(company_id, normalized_value, contact_id)
  WHERE normalized_value IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contact_email_company_norm_contact
  ON contact_email_address(company_id, normalized_value, contact_id)
  WHERE normalized_value IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contact_email_company_lower_norm_contact
  ON contact_email_address(company_id, lower(btrim(normalized_value)), contact_id)
  WHERE normalized_value IS NOT NULL;

UPDATE contact_email_address
SET normalized_value = lower(btrim(COALESCE(normalized_value, original_value)))
WHERE COALESCE(normalized_value, original_value) IS NOT NULL
  AND normalized_value IS DISTINCT FROM lower(btrim(COALESCE(normalized_value, original_value)));

UPDATE contact
SET normalized_email = lower(btrim(COALESCE(normalized_email, primary_email, email)))
WHERE COALESCE(normalized_email, primary_email, email) IS NOT NULL
  AND normalized_email IS DISTINCT FROM lower(btrim(COALESCE(normalized_email, primary_email, email)));

-- Conservative phone backfill. Ambiguous or invalid historical values remain
-- NULL and therefore cannot become merge evidence.
UPDATE contact
SET normalized_phone = CASE
  WHEN regexp_replace(COALESCE(primary_phone, phone), '[^0-9]', '', 'g') ~ '^[0-9]{10}$'
    THEN '+1' || regexp_replace(COALESCE(primary_phone, phone), '[^0-9]', '', 'g')
  WHEN regexp_replace(COALESCE(primary_phone, phone), '[^0-9]', '', 'g') ~ '^[1-9][0-9]{10,14}$'
    THEN '+' || regexp_replace(COALESCE(primary_phone, phone), '[^0-9]', '', 'g')
  ELSE NULL
END
WHERE normalized_phone IS NULL
  AND COALESCE(primary_phone, phone) IS NOT NULL;

-- Preserve poisoned/untrusted legacy values for audit, but make them
-- ineligible for canonical display and automatic identity evidence.
WITH questionable AS (
  SELECT c.id,
         COALESCE(
           NULLIF(btrim(c.display_name), ''),
           NULLIF(btrim(c.name), ''),
           NULLIF(btrim(concat_ws(' ', c.first_name, c.last_name)), '')
         ) AS legacy_name
  FROM contact c
  WHERE COALESCE(
          NULLIF(btrim(c.display_name), ''),
          NULLIF(btrim(c.name), ''),
          NULLIF(btrim(concat_ws(' ', c.first_name, c.last_name)), '')
        ) IS NOT NULL
    AND (
      c.identity_status <> 'confirmed'
      OR c.name_verification_level NOT IN ('verified', 'trusted')
      OR c.name_source NOT IN ('customer_confirmed', 'pwa_verified', 'manual', 'google_contacts')
      OR COALESCE(c.name_provenance->>'source', '') IN ('', 'inbound_sms', 'legacy', 'unverified_import')
      OR lower(btrim(COALESCE(c.display_name, c.name, concat_ws(' ', c.first_name, c.last_name)))) IN
         ('yes','y','confirm','confirmed','no','n','incorrect','change','stop','start','help',
          'ok','okay','thanks','thank you','sounds good','got it','unknown','unknown contact',
          'pending identity','name needed')
      OR COALESCE(c.display_name, c.name, concat_ws(' ', c.first_name, c.last_name)) ~* '(https?://|www\.|@)'
      OR COALESCE(c.display_name, c.name, concat_ws(' ', c.first_name, c.last_name)) ~* '^[[:space:][:punct:][:digit:]]+$'
      OR COALESCE(c.display_name, c.name, concat_ws(' ', c.first_name, c.last_name)) ~*
         '^((can|could|would)[[:space:]]+you[[:space:]]+|(where|when|why|how)[[:space:]]+(are|is|do|does|can|could|would|will)[[:space:]]+|please[[:space:]]+(send|call|text|help|tell|schedule|book|cancel|change|provide)[[:space:]]+|i[[:space:]]+(need|want|would[[:space:]]+like|am|have)[[:space:]]+|need[[:space:]]+(more[[:space:]]+)?(help|information|details|service|support))'
      OR COALESCE(c.display_name, c.name, concat_ws(' ', c.first_name, c.last_name)) ~*
         '(^|[[:space:]])(appointment|availability|booking|details|estimate|hours|information|message|order|price|quote|schedule|service|status)($|[[:space:]])'
    )
)
UPDATE contact c
SET name_provenance = COALESCE(c.name_provenance, '{}'::jsonb)
      || jsonb_build_object(
           'legacy_untrusted_name',
           COALESCE(c.name_provenance->>'legacy_untrusted_name', q.legacy_name),
           'legacy_reviewed_by', '20260727_identity_hardening',
           'source', 'legacy'
         ),
    name_verification_level = 'unverified',
    name_source = 'legacy_untrusted',
    display_name = NULL,
    identity_status = CASE
      WHEN c.identity_status = 'confirmed' THEN 'awaiting_name'
      ELSE c.identity_status
    END,
    identity_verified_at = CASE
      WHEN c.identity_status = 'confirmed' THEN NULL
      ELSE c.identity_verified_at
    END
FROM questionable q
WHERE c.id = q.id;

-- Confirmation rows without a current, tenant/contact-bound nonce and
-- timestamp are not safe to promote. Reset them without deleting history.
UPDATE contact c
SET name_provenance = COALESCE(c.name_provenance, '{}'::jsonb)
      || jsonb_build_object(
           'stale_pending_identity',
           jsonb_build_object(
             'first_name', c.pending_first_name,
             'last_name', c.pending_last_name,
             'email', c.pending_email,
             'reset_by', '20260727_identity_hardening'
           )
         ),
    pending_first_name = NULL,
    pending_last_name = NULL,
    pending_email = NULL,
    identity_status = 'awaiting_name',
    identity_requested_fields = '["first_name","last_name","email"]'::jsonb,
    identity_request_state = '{}'::jsonb,
    identity_last_request_sid = NULL
WHERE c.identity_status = 'awaiting_confirmation'
  AND (
    c.identity_fields_requested_at IS NULL
    OR c.identity_fields_requested_at < NOW() - INTERVAL '24 hours'
    OR c.identity_last_request_sid IS NULL
    OR COALESCE(c.identity_request_state->>'phase', '') <> 'awaiting_confirmation'
    OR COALESCE(c.identity_request_state->>'confirmation_nonce', '') <> c.identity_last_request_sid
    OR COALESCE(c.identity_request_state->>'company_id', '') <> c.company_id::text
    OR COALESCE(c.identity_request_state->>'contact_id', '') <> c.id::text
    OR CASE
      WHEN COALESCE(c.identity_request_state->>'requested_at', '') ~
           '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?$'
      THEN (
        (c.identity_request_state->>'requested_at')::timestamp
          < NOW()::timestamp - INTERVAL '24 hours'
        OR (c.identity_request_state->>'requested_at')::timestamp
          > NOW()::timestamp + INTERVAL '5 minutes'
        OR abs(extract(epoch FROM (
          (c.identity_request_state->>'requested_at')::timestamp
          - c.identity_fields_requested_at
        ))) > 2
      )
      ELSE TRUE
    END
  );
