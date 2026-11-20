-- Canonical SMS audience snapshots and scheduled execution audit fields.
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS selected_tag_ids JSON;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS scheduled_preview_count INTEGER;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS scheduled_unique_phone_count INTEGER;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS scheduled_eligible_recipient_count INTEGER;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS execution_recipient_count INTEGER;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS execution_count_delta INTEGER;
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS scheduled_timezone VARCHAR(80) DEFAULT 'UTC';
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS scheduled_by_user_id INTEGER REFERENCES "user"(id);
ALTER TABLE sms_campaign ADD COLUMN IF NOT EXISTS recipient_resolution JSON;

-- This number is already tenant-owned in twilio_phone_number; preserve that
-- company assignment and enable it as an authorized campaign sender.
UPDATE twilio_phone_number
SET phone_number = '+19165989519', is_active = TRUE, sms_enabled = TRUE,
    campaign_sender_enabled = TRUE, updated_at = CURRENT_TIMESTAMP
WHERE regexp_replace(phone_number, '[^0-9]', '', 'g') IN ('9165989519', '19165989519');

-- Canonicalize the legacy MyOrder tag per tenant before campaigns reference it.
INSERT INTO segment (company_id, name, segment_type, match_mode, is_active, created_at, updated_at)
SELECT DISTINCT c.company_id, 'MyOrder Customer', 'contact_tag', 'all', TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
FROM contact c
WHERE lower(regexp_replace(c.tags, '\\s+', ' ', 'g')) LIKE '%myorder customer%'
  AND NOT EXISTS (
    SELECT 1 FROM segment s WHERE s.company_id = c.company_id
      AND lower(regexp_replace(trim(s.name), '\\s+', ' ', 'g')) = 'myorder customer'
  );

UPDATE sms_campaign campaign
SET selected_tag_ids = json_build_array(segment.id),
audience_filter = COALESCE(campaign.audience_filter, '{}'::jsonb)
    || jsonb_build_object(
        'selected_tag_ids',
        jsonb_build_array(segment.id)
    )
FROM segment
WHERE campaign.company_id = segment.company_id
  AND lower(regexp_replace(trim(campaign.segment), '\\s+', ' ', 'g')) = 'myorder customer'
  AND lower(regexp_replace(trim(segment.name), '\\s+', ' ', 'g')) = 'myorder customer';
