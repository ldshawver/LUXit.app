-- Per-user, per-tenant "Receive Calls" routing preference. Forward-only,
-- idempotent, additive.
--
-- user_company_access.receive_calls
--   TRUE  = route this tenant's shared LUXit calls to the user's eligible PWA
--           devices (auto-registered over Wi-Fi or cellular data).
--   FALSE = the user's PWA does not register for / present inbound calls and
--           server-side inbound routing excludes the user, regardless of
--           phone_availability. Voicemail / no-answer fallback is unaffected.
--
-- Independent of phone_availability (AWAY suppresses shared-call attention
-- without changing this preference).
--
-- Compatibility default: TRUE. Existing routing rings any approved PWA device
-- for an active membership unless the user is Away, so every currently
-- participating membership must keep that behavior -- hence DEFAULT true and no
-- backfill for active rows.
--
-- Inactive / archived memberships are set FALSE: they cannot receive calls
-- anyway (routing filters is_active), and archive_user_for_company() should
-- leave the flag off so a later restore is an explicit re-enable.

ALTER TABLE user_company_access
    ADD COLUMN IF NOT EXISTS receive_calls BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE user_company_access
    ADD COLUMN IF NOT EXISTS receive_calls_changed_at TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE user_company_access
    ADD COLUMN IF NOT EXISTS receive_calls_changed_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE user_company_access
    ADD COLUMN IF NOT EXISTS receive_calls_source VARCHAR(16);

-- Inactive/archived memberships: preference off (no-op on re-run once set).
UPDATE user_company_access uca
SET receive_calls = false,
    receive_calls_source = 'system',
    receive_calls_changed_at = COALESCE(receive_calls_changed_at, now())
FROM "user" u
WHERE u.id = uca.user_id
  AND uca.receive_calls = true
  AND (uca.is_active = false OR u.active = false);

CREATE INDEX IF NOT EXISTS ix_user_company_access_receive_calls
    ON user_company_access (company_id, receive_calls)
    WHERE receive_calls = true;
