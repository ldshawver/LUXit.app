-- Forward-only, rerunnable lifecycle support for real browser push devices.
ALTER TABLE pwa_device ADD COLUMN IF NOT EXISTS lifecycle_status VARCHAR(20) NOT NULL DEFAULT 'pending';
ALTER TABLE pwa_device ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMP;
ALTER TABLE pwa_device ADD COLUMN IF NOT EXISTS expired_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_pwa_device_company_lifecycle
  ON pwa_device(company_id, lifecycle_status);

-- Existing enabled devices remain usable; records without push stay pending.
UPDATE pwa_device
SET lifecycle_status = CASE
  WHEN approved_status = 'pending' THEN 'pending'
  WHEN approved_status = 'revoked' OR push_enabled = FALSE THEN 'disabled'
  WHEN push_enabled = TRUE AND approved_status = 'approved' THEN 'active'
  ELSE 'pending'
END
WHERE lifecycle_status IS NULL OR lifecycle_status = 'pending';
