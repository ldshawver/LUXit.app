-- Add durable segment targeting support for SMS campaigns.
-- Safe/idempotent for production: does not touch existing rows beyond schema metadata.
ALTER TABLE sms_campaign
    ADD COLUMN IF NOT EXISTS segment VARCHAR(100);
