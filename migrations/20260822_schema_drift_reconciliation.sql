-- Reconcile luxdb_dev/models.py schema drift discovered 2026-08-22.
--
-- Root cause: these columns/indexes were added to models.py across several
-- feature commits (communications-hub per-user licensing on
-- user_company_access, TikTok OAuth creator profile fields, demo_request
-- Google/avatar enrichment, auto_reply_rule per-line targeting, push
-- subscription device identity, google_oauth_token sync diagnostics, and a
-- batch of Contact/CRM lookup indexes) without a corresponding migration
-- file ever being written, and the startup self-heal safety net in app.py
-- only covers tables explicitly listed in its hardcoded dict -- it never
-- included user_company_access or several of the others below. This is a
-- pure coverage gap, not a corrupted ledger: no prior migration or ledger
-- entry claims to already provide this schema.
--
-- All statements are additive and IF NOT EXISTS-guarded, safe to run
-- against a database that already has some (but not all) of this schema.

BEGIN;

-- ── user_company_access: per-user Communications Hub licensing/toggles ──────
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS comms_hub_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS pwa_access_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS calls_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS sms_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS voicemail_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS ai_comms_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS forwarding_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS communications_license BOOLEAN DEFAULT FALSE;
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS assigned_number VARCHAR(20);
ALTER TABLE user_company_access ADD COLUMN IF NOT EXISTS number_type VARCHAR(20) DEFAULT 'shared';

-- ── tiktok_oauth: creator profile snapshot ──────────────────────────────────
ALTER TABLE tiktok_oauth ADD COLUMN IF NOT EXISTS creator_username VARCHAR(255);
ALTER TABLE tiktok_oauth ADD COLUMN IF NOT EXISTS creator_nickname VARCHAR(255);
ALTER TABLE tiktok_oauth ADD COLUMN IF NOT EXISTS creator_info JSON;
ALTER TABLE tiktok_oauth ADD COLUMN IF NOT EXISTS disconnected_at TIMESTAMP WITHOUT TIME ZONE;

-- ── demo_request: Google/avatar enrichment fields ───────────────────────────
ALTER TABLE demo_request ADD COLUMN IF NOT EXISTS normalized_phone VARCHAR(32);
ALTER TABLE demo_request ADD COLUMN IF NOT EXISTS external_google_contact_id VARCHAR(255);
ALTER TABLE demo_request ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500);

-- ── auto_reply_rule: per-line targeting ─────────────────────────────────────
ALTER TABLE auto_reply_rule ADD COLUMN IF NOT EXISTS phone_number_id INTEGER REFERENCES twilio_phone_number(id);

-- ── push_subscription: device identity ──────────────────────────────────────
ALTER TABLE push_subscription ADD COLUMN IF NOT EXISTS device_key VARCHAR(120);

-- ── google_oauth_token: sync diagnostics ────────────────────────────────────
ALTER TABLE google_oauth_token ADD COLUMN IF NOT EXISTS sync_error TEXT;

-- ── Missing indexes ──────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS ix_contact_last_contacted_at ON contact(last_contacted_at);
CREATE INDEX IF NOT EXISTS ix_contact_lifecycle_stage ON contact(lifecycle_stage);
CREATE INDEX IF NOT EXISTS ix_contact_status ON contact(status);
CREATE INDEX IF NOT EXISTS ix_contact_duplicate_status ON contact(duplicate_status);
CREATE INDEX IF NOT EXISTS ix_contact_next_follow_up_at ON contact(next_follow_up_at);
CREATE INDEX IF NOT EXISTS ix_contact_google_contact_resource_id ON contact(google_contact_resource_id);
CREATE INDEX IF NOT EXISTS ix_contact_original_source ON contact(original_source);
CREATE INDEX IF NOT EXISTS ix_contact_possible_duplicate_of_id ON contact(possible_duplicate_of_id);
CREATE INDEX IF NOT EXISTS ix_contact_last_activity_at ON contact(last_activity_at);
CREATE INDEX IF NOT EXISTS ix_contact_google_match_status ON contact(google_match_status);
CREATE INDEX IF NOT EXISTS ix_contact_tenant_id ON contact(tenant_id);
CREATE INDEX IF NOT EXISTS ix_contact_owner_user_id ON contact(owner_user_id);
CREATE INDEX IF NOT EXISTS ix_contact_normalized_phone ON contact(normalized_phone);
CREATE INDEX IF NOT EXISTS ix_contact_imported_list ON contact(imported_list);
CREATE INDEX IF NOT EXISTS ix_contact_identity_status ON contact(identity_status);
CREATE INDEX IF NOT EXISTS ix_contact_merged_into_contact_id ON contact(merged_into_contact_id);
CREATE INDEX IF NOT EXISTS ix_contact_external_google_contact_id ON contact(external_google_contact_id);
CREATE INDEX IF NOT EXISTS ix_contact_identity_conflict_status ON contact(identity_conflict_status);
CREATE INDEX IF NOT EXISTS ix_contact_imported_batch_id ON contact(imported_batch_id);
CREATE INDEX IF NOT EXISTS ix_contact_normalized_email ON contact(normalized_email);
CREATE INDEX IF NOT EXISTS ix_contact_source_channel ON contact(source_channel);
CREATE INDEX IF NOT EXISTS ix_contact_lead_status ON contact(lead_status);
CREATE INDEX IF NOT EXISTS ix_contact_approval_status ON contact(approval_status);
CREATE INDEX IF NOT EXISTS ix_contact_source_phone_number ON contact(source_phone_number);
CREATE INDEX IF NOT EXISTS ix_contact_latest_source ON contact(latest_source);
CREATE INDEX IF NOT EXISTS ix_sms_template_company_id ON sms_template(company_id);
CREATE INDEX IF NOT EXISTS ix_calendar_event_company_id ON calendar_event(company_id);
CREATE INDEX IF NOT EXISTS ix_demo_request_normalized_phone ON demo_request(normalized_phone);
CREATE INDEX IF NOT EXISTS ix_demo_request_external_google_contact_id ON demo_request(external_google_contact_id);
CREATE INDEX IF NOT EXISTS ix_twilio_conversation_phone_number_id ON twilio_conversation(phone_number_id);
CREATE INDEX IF NOT EXISTS ix_auto_reply_rule_phone_number_id ON auto_reply_rule(phone_number_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_phone_settings_company_id ON phone_settings(company_id);
CREATE INDEX IF NOT EXISTS ix_push_subscription_is_active ON push_subscription(is_active);
CREATE INDEX IF NOT EXISTS ix_push_subscription_device_key ON push_subscription(device_key);
CREATE INDEX IF NOT EXISTS ix_pwa_device_lifecycle_status ON pwa_device(lifecycle_status);

COMMIT;
