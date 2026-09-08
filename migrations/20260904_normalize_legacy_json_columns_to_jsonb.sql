-- Normalize three legacy columns from `json` to `jsonb`. Forward-only,
-- idempotent, schema-only.
--
--   contact.name_provenance
--   sms_campaign.audience_filter
--   twilio_phone_number.business_hours
--
-- ── Why ────────────────────────────────────────────────────────────────────
-- Three already-shipped migrations write these columns with jsonb operators
-- and literals:
--   20260622_after_hours_sms_canonical.sql          business_hours = '{}'::jsonb ...
--   20260720_sms_campaign_recipient_resolution.sql  audience_filter = COALESCE(...,'{}'::jsonb) || jsonb_build_object(...)
--   20260727_inbound_identity_resolution_hardening  name_provenance = COALESCE(...,'{}'::jsonb) || jsonb_build_object(...)
-- Those forms are only valid when the column IS jsonb (`json = jsonb`,
-- `COALESCE(json, jsonb)` and `json || jsonb` all raise
-- "operator does not exist" / "types cannot be matched"). Production has held
-- these columns as `jsonb` (with a `'{}'::jsonb` default) since before this
-- repo's ledger began, so those migrations have always succeeded there and on
-- any environment seeded from a production snapshot. But the ORM models
-- declared plain `JSON`, so a schema built fresh from the models (a brand-new
-- environment, or the disposable-DB migration-chain gate) got `json` columns
-- and those three migrations fail. Staging (built from the old models) still
-- has `json` here and was worked around once with an ad-hoc, never-committed
-- edit to those migration files (2026-08-23) -- the source of the staging
-- ledger checksum drift reconciled alongside this change.
--
-- This fixes the root cause once, at the column type, so every environment
-- matches what production has always actually had. The ORM models are updated
-- in the same commit to declare `jsonb` on PostgreSQL, so a fresh
-- `create_all()` produces `jsonb` columns; this migration then (re)asserts the
-- `'{}'::jsonb` server default on top (the canonical build is always
-- `create_all()` followed by the full migration chain).
--
-- ── Safety ─────────────────────────────────────────────────────────────────
--   * `ALTER ... TYPE jsonb USING col::jsonb` is the standard lossless widening:
--     the stored logical document is unchanged and NULL stays NULL
--     (NULL::jsonb IS NULL).
--   * No-op when a column is already `jsonb` (guarded on data_type).
--   * The `'{}'::jsonb` default is (re)asserted to match production exactly --
--     today a fresh/staging `name_provenance` is NOT NULL with no default,
--     a latent failure for any non-ORM insert. No nullability change.
--   * FAILS LOUD if a target column is present but is neither `json` nor
--     `jsonb` -- we never blind-cast an unexpected type.
--   * Touches nothing else: no other column, no index, no constraint, no data.

DO $$
DECLARE
    r RECORD;
    v_type text;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('contact',             'name_provenance'),
            ('sms_campaign',        'audience_filter'),
            ('twilio_phone_number', 'business_hours')
        ) AS t(table_name, column_name)
    LOOP
        SELECT data_type INTO v_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = r.table_name
          AND column_name = r.column_name;

        IF v_type IS NULL THEN
            -- Column not present in this environment; nothing to normalize.
            CONTINUE;
        END IF;

        IF v_type NOT IN ('json', 'jsonb') THEN
            RAISE EXCEPTION
                'normalize_legacy_json_columns_to_jsonb: %.% has unexpected type "%" (expected json or jsonb)',
                r.table_name, r.column_name, v_type;
        END IF;

        IF v_type = 'json' THEN
            EXECUTE format(
                'ALTER TABLE %I ALTER COLUMN %I TYPE jsonb USING %I::jsonb',
                r.table_name, r.column_name, r.column_name
            );
        END IF;

        -- Match production: a '{}'::jsonb server default on all three.
        -- Idempotent -- ALTER COLUMN SET DEFAULT to the same value is a no-op.
        EXECUTE format(
            'ALTER TABLE %I ALTER COLUMN %I SET DEFAULT ''{}''::jsonb',
            r.table_name, r.column_name
        );
    END LOOP;
END $$;
