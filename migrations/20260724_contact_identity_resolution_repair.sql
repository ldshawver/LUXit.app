-- Forward-only, rerunnable contact identity/name resolution state.
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_verified_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_verification_source VARCHAR(80);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_requested_fields JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_request_state JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_fields_requested_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_last_request_sid VARCHAR(64);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_last_response_sid VARCHAR(64);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_conflict_status VARCHAR(32) NOT NULL DEFAULT 'none';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS identity_conflict_details JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS name_verification_level VARCHAR(32) NOT NULL DEFAULT 'unverified';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS name_verified_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS name_verified_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS name_provenance JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS approval_status VARCHAR(32) NOT NULL DEFAULT 'pending';
ALTER TABLE contact ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP;
ALTER TABLE contact ADD COLUMN IF NOT EXISTS approved_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS approval_source VARCHAR(80);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS approval_match_source VARCHAR(80);
ALTER TABLE contact ADD COLUMN IF NOT EXISTS approval_rejected_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_contact_company_approval_status ON contact(company_id, approval_status);
CREATE INDEX IF NOT EXISTS ix_contact_company_identity_conflict ON contact(company_id, identity_conflict_status);
CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_phone_active ON contact(company_id, normalized_phone) WHERE is_active IS TRUE AND normalized_phone IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_email_active ON contact(company_id, normalized_email) WHERE is_active IS TRUE AND normalized_email IS NOT NULL;

-- Remove only proven placeholders/phone-shaped display values; never invent a name.
UPDATE contact SET display_name = NULL
WHERE display_name IS NOT NULL AND (
  lower(btrim(display_name)) IN ('pending identity','unknown','unknown contact','n/a','na')
  OR btrim(display_name) = ''
  OR (COALESCE(normalized_phone, phone) IS NOT NULL
      AND length(regexp_replace(COALESCE(normalized_phone, phone), '[^0-9]', '', 'g')) >= 7
      AND regexp_replace(display_name, '[^0-9]', '', 'g') = regexp_replace(COALESCE(normalized_phone, phone), '[^0-9]', '', 'g'))
);

-- A reliable, unique, non-placeholder stored name repairs stale workflow state.
UPDATE contact c SET
  identity_status = 'confirmed',
  identity_verified_at = COALESCE(c.identity_verified_at, c.identity_confirmed_at, NOW()),
  identity_verification_source = COALESCE(c.identity_verification_source, c.name_source, 'canonical_repair'),
  name_verification_level = CASE WHEN c.name_source IN ('customer_confirmed','manual','user') THEN 'verified' ELSE 'trusted' END
WHERE c.identity_status IN ('pending_identity','awaiting_confirmation')
  AND COALESCE(c.identity_conflict_status, 'none') = 'none'
  AND COALESCE(NULLIF(btrim(c.display_name), ''), NULLIF(btrim(c.name), ''), NULLIF(btrim(concat_ws(' ', c.first_name, c.last_name)), '')) IS NOT NULL
  AND lower(COALESCE(NULLIF(btrim(c.display_name), ''), NULLIF(btrim(c.name), ''), NULLIF(btrim(concat_ws(' ', c.first_name, c.last_name)), ''))) NOT IN ('pending identity','unknown','unknown contact','n/a','na')
  AND COALESCE(NULLIF(btrim(c.display_name), ''), NULLIF(btrim(c.name), ''), NULLIF(btrim(concat_ws(' ', c.first_name, c.last_name)), '')) ~ '[[:alpha:]]'
  AND position('@' in COALESCE(NULLIF(btrim(c.display_name), ''), NULLIF(btrim(c.name), ''), NULLIF(btrim(concat_ws(' ', c.first_name, c.last_name)), ''))) = 0
  AND NOT EXISTS (SELECT 1 FROM contact d WHERE d.company_id = c.company_id AND d.is_active IS TRUE AND d.id <> c.id AND d.normalized_phone = c.normalized_phone);
