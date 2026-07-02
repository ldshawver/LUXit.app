ALTER TABLE company ADD COLUMN IF NOT EXISTS require_approved_pwa_devices BOOLEAN DEFAULT FALSE;

ALTER TABLE pwa_device ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP;
ALTER TABLE pwa_device ADD COLUMN IF NOT EXISTS last_ip VARCHAR(64);
ALTER TABLE pwa_device ADD COLUMN IF NOT EXISTS approved_status VARCHAR(20) DEFAULT 'pending';
ALTER TABLE pwa_device ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP;
ALTER TABLE pwa_device ADD COLUMN IF NOT EXISTS approved_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE pwa_device ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMP;
ALTER TABLE pwa_device ADD COLUMN IF NOT EXISTS revoked_by_user_id INTEGER REFERENCES "user"(id);
CREATE INDEX IF NOT EXISTS ix_pwa_device_company_status ON pwa_device(company_id, approved_status);
