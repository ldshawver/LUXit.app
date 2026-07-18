ALTER TABLE "user" ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP NULL;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS archived_by_user_id INTEGER NULL REFERENCES "user"(id) ON DELETE SET NULL;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS archived_company_id INTEGER NULL REFERENCES company(id) ON DELETE SET NULL;

ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP NULL;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS archived_by_user_id INTEGER NULL REFERENCES "user"(id) ON DELETE SET NULL;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS previous_role VARCHAR(20) NULL;

CREATE INDEX IF NOT EXISTS ix_user_active_archived ON "user"(active, archived_at);
CREATE INDEX IF NOT EXISTS ix_user_archived_company ON "user"(archived_company_id, archived_at);
CREATE INDEX IF NOT EXISTS ix_user_company_access_active_company ON user_company_access(company_id, is_active, user_id);

UPDATE "user" SET active = TRUE WHERE active IS NULL;
UPDATE user_company_access SET is_active = TRUE WHERE is_active IS NULL;
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS session_revoked_at TIMESTAMP NULL;
