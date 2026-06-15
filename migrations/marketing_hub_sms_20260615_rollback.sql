-- Conservative rollback for Marketing Hub SMS/Social durable schema migration.
-- This rollback drops only migration-owned tables and migration-named indexes.
-- It intentionally does NOT drop shared columns added to existing tables because
-- some production databases may already have those columns from earlier startup
-- backfills or manual hotfixes.

ALTER TABLE IF EXISTS marketing_audit_log DROP CONSTRAINT IF EXISTS fk_marketing_audit_log_created_by;
ALTER TABLE IF EXISTS marketing_audit_log DROP CONSTRAINT IF EXISTS fk_marketing_audit_log_company;
ALTER TABLE IF EXISTS sms_auto_reply_rule DROP CONSTRAINT IF EXISTS fk_sms_auto_reply_rule_campaign;
ALTER TABLE IF EXISTS sms_auto_reply_rule DROP CONSTRAINT IF EXISTS fk_sms_auto_reply_rule_company;
ALTER TABLE IF EXISTS sms_keyword_rule DROP CONSTRAINT IF EXISTS ck_sms_keyword_rule_match_type;
ALTER TABLE IF EXISTS sms_keyword_rule DROP CONSTRAINT IF EXISTS fk_sms_keyword_rule_campaign;
ALTER TABLE IF EXISTS sms_keyword_rule DROP CONSTRAINT IF EXISTS fk_sms_keyword_rule_company;
ALTER TABLE IF EXISTS social_post DROP CONSTRAINT IF EXISTS fk_social_post_user;
ALTER TABLE IF EXISTS social_post DROP CONSTRAINT IF EXISTS fk_social_post_company;
ALTER TABLE IF EXISTS sms_recipient DROP CONSTRAINT IF EXISTS ck_sms_recipient_status;
ALTER TABLE IF EXISTS sms_recipient DROP CONSTRAINT IF EXISTS fk_sms_recipient_contact;
ALTER TABLE IF EXISTS sms_recipient DROP CONSTRAINT IF EXISTS fk_sms_recipient_campaign;
ALTER TABLE IF EXISTS sms_recipient DROP CONSTRAINT IF EXISTS fk_sms_recipient_company;
ALTER TABLE IF EXISTS sms_campaign DROP CONSTRAINT IF EXISTS ck_sms_campaign_status;
ALTER TABLE IF EXISTS sms_campaign DROP CONSTRAINT IF EXISTS fk_sms_campaign_created_by;
ALTER TABLE IF EXISTS sms_campaign DROP CONSTRAINT IF EXISTS fk_sms_campaign_company;
DROP TRIGGER IF EXISTS trg_marketing_audit_log_append_only ON marketing_audit_log;
DROP FUNCTION IF EXISTS prevent_marketing_audit_log_mutation;
DROP INDEX IF EXISTS ux_sms_recipient_campaign_contact;
DROP INDEX IF EXISTS ux_sms_recipient_provider_message_sid_not_null;
DROP INDEX IF EXISTS ix_twilio_phone_number_company_phone;
DROP INDEX IF EXISTS ix_twilio_conversation_company_from;
DROP INDEX IF EXISTS ix_contact_company_phone;
DROP INDEX IF EXISTS ix_marketing_audit_log_entity;
DROP INDEX IF EXISTS ix_marketing_audit_log_company_id;
DROP TABLE IF EXISTS marketing_audit_log;

DROP INDEX IF EXISTS ix_sms_auto_reply_rule_campaign_id;
DROP INDEX IF EXISTS ix_sms_auto_reply_rule_company_id;
DROP TABLE IF EXISTS sms_auto_reply_rule;

DROP INDEX IF EXISTS ix_sms_keyword_rule_priority;
DROP INDEX IF EXISTS ix_sms_keyword_rule_campaign_id;
DROP INDEX IF EXISTS ix_sms_keyword_rule_company_id;
DROP TABLE IF EXISTS sms_keyword_rule;

DROP INDEX IF EXISTS ix_social_post_company_id;
DROP INDEX IF EXISTS ix_sms_recipient_provider_message_sid;
DROP INDEX IF EXISTS ix_sms_recipient_contact_id;
DROP INDEX IF EXISTS ix_sms_recipient_campaign_id;
DROP INDEX IF EXISTS ix_sms_recipient_company_id;
DROP INDEX IF EXISTS ix_sms_campaign_company_status;
DROP INDEX IF EXISTS ix_sms_campaign_company_id;
