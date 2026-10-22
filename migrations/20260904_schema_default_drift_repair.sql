-- Staging/schema default-drift repair. Forward-only, idempotent.
--
-- Audit findings (read-only, staging):
--   A. user_company_access.is_default is the sole invariant actually consulted
--      for tenant resolution (User.get_default_company() reads
--      user.default_company_id / Company FK, never user_company.c.is_default).
--      No functional/tenant-isolation bug found -- but user_company.is_default
--      is still live-written by several routes (add_company/company-switch) and
--      had drifted out of sync with user_company_access.is_default for at least
--      one row. Reconciled below so the two tables agree; user_company_access
--      remains canonical.
--   B. Company has 6 NOT NULL columns with a Python-side (ORM) default but no
--      Postgres-side server_default. Every current Company(...) construction
--      site uses the ORM (never raw SQL), so this has not caused a live
--      failure -- but it is a latent footgun for any future raw-SQL/migration
--      insert. Server defaults added below as defense in depth.
--   C. phone_settings has no default row for most companies. Every current
--      reader already null-checks it and falls back safely (grep-verified) --
--      not a crash risk -- but backfilling one default row per active company
--      removes the gap outright rather than relying on every future caller
--      remembering the null-check.
--
-- Nothing here changes tenant-resolution logic, self-heal behavior, or any
-- column read for an authoritative decision.

-- A. Reconcile user_company.is_default to match user_company_access.is_default
--    for rows present in both tables. One-time data correction; a no-op on
--    any pair already in sync.
UPDATE user_company AS uc
SET is_default = uca.is_default
FROM user_company_access AS uca
WHERE uc.user_id = uca.user_id
  AND uc.company_id = uca.company_id
  AND uca.is_active IS DISTINCT FROM false
  AND uc.is_default IS DISTINCT FROM uca.is_default;

-- B. Server-side defaults for Company NOT NULL columns (defense in depth;
--    existing rows are already populated via the ORM default, so this only
--    changes behavior for a future insert that bypasses the ORM).
ALTER TABLE company ALTER COLUMN require_approved_pwa_devices SET DEFAULT false;
ALTER TABLE company ALTER COLUMN sync_confirmed_contacts_to_google SET DEFAULT false;
ALTER TABLE company ALTER COLUMN setup_fee_paid SET DEFAULT false;
ALTER TABLE company ALTER COLUMN contacts_used SET DEFAULT 0;
ALTER TABLE company ALTER COLUMN contacts_overage SET DEFAULT 0;
ALTER TABLE company ALTER COLUMN last_reported_contact_usage SET DEFAULT 0;

-- C. Backfill a default phone_settings row for every active company that has
--    none. Model column defaults only -- no behavior change for a company
--    that already has a row (during_hours_route='ring_pwa',
--    after_hours_route='voicemail', timezone='America/Los_Angeles', a 25s
--    ring duration, missed-call/after-hours SMS left off).
INSERT INTO phone_settings (company_id, business_hours, timezone, during_hours_route,
                             after_hours_route, ring_duration_seconds,
                             missed_call_sms_enabled, after_hours_sms_enabled)
SELECT c.id, '{}'::json, 'America/Los_Angeles', 'ring_pwa', 'voicemail', 25, false, false
FROM company c
WHERE c.is_active = true
  AND NOT EXISTS (SELECT 1 FROM phone_settings ps WHERE ps.company_id = c.id);
