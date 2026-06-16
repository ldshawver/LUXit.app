-- Rollback for 20260613_sms_campaign_compliance.sql (PostgreSQL).
DROP INDEX IF EXISTS ix_sms_recipient_campaign_status;
DROP INDEX IF EXISTS ix_sms_campaign_company_id;
ALTER TABLE twilio_account DROP COLUMN IF EXISTS after_hours_cooldown_minutes;
ALTER TABLE sms_recipient DROP COLUMN IF EXISTS error_code;
ALTER TABLE sms_recipient DROP COLUMN IF EXISTS message_sid;
ALTER TABLE sms_recipient DROP COLUMN IF EXISTS phone_number;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS test_sent_at;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS estimated_recipient_count;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS audience_filter;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS company_id;
ALTER TABLE contact DROP COLUMN IF EXISTS sms_consent_status;
ALTER TABLE contact DROP COLUMN IF EXISTS sms_opt_out_at;
ALTER TABLE contact DROP COLUMN IF EXISTS sms_marketing_opt_in_source;
ALTER TABLE contact DROP COLUMN IF EXISTS sms_marketing_opt_in_at;
ALTER TABLE contact DROP COLUMN IF EXISTS sms_marketing_opt_in;
