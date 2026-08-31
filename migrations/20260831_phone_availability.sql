-- Phone Availability (shared-line real-time participation) — forward-only, idempotent.
-- AWAY pauses ringing / incoming-call UI / phone push + badges for a user on a
-- tenant's shared lines. NOT an account disable: independent of is_active/role/
-- membership/consent/number. Default 'available' preserves current behaviour.

ALTER TABLE user_company_access
  ADD COLUMN IF NOT EXISTS phone_availability VARCHAR(16) NOT NULL DEFAULT 'available';

ALTER TABLE user_company_access
  ADD COLUMN IF NOT EXISTS phone_availability_changed_at TIMESTAMP WITHOUT TIME ZONE;

ALTER TABLE user_company_access
  ADD COLUMN IF NOT EXISTS phone_availability_changed_by_user_id INTEGER;

ALTER TABLE user_company_access
  ADD COLUMN IF NOT EXISTS phone_availability_source VARCHAR(16);

CREATE INDEX IF NOT EXISTS ix_uca_company_phone_availability
  ON user_company_access (company_id, phone_availability)
  WHERE is_active IS TRUE;
