-- Canonicalize after-hours SMS settings on twilio_phone_number.
ALTER TABLE twilio_phone_number ADD COLUMN IF NOT EXISTS after_hours_cooldown_minutes INTEGER DEFAULT 720;
ALTER TABLE twilio_message ADD COLUMN IF NOT EXISTS auto_responded BOOLEAN DEFAULT FALSE;

UPDATE twilio_phone_number pn
SET after_hours_text = COALESCE(NULLIF(pn.after_hours_text, ''), NULLIF(ps.after_hours_sms_body, ''), NULLIF(ta.after_hours_text, ''), 'Thanks for reaching out. Our business hours are daily from 2 PM to 2 AM. We’ll respond as soon as we’re back online.'),
    after_hours_cooldown_minutes = COALESCE(pn.after_hours_cooldown_minutes, ta.after_hours_cooldown_minutes, 720),
    business_hours = CASE
        WHEN pn.business_hours IS NULL OR pn.business_hours = '{}'::jsonb THEN '{"0":{"is_open":true,"open":"14:00","close":"02:00"},"1":{"is_open":true,"open":"14:00","close":"02:00"},"2":{"is_open":true,"open":"14:00","close":"02:00"},"3":{"is_open":true,"open":"14:00","close":"02:00"},"4":{"is_open":true,"open":"14:00","close":"02:00"},"5":{"is_open":true,"open":"14:00","close":"02:00"},"6":{"is_open":true,"open":"14:00","close":"02:00"}}'::jsonb
        ELSE pn.business_hours
    END
FROM twilio_account ta
LEFT JOIN phone_settings ps ON ps.company_id = pn.company_id
WHERE ta.id = pn.twilio_account_id OR ta.company_id = pn.company_id;

UPDATE twilio_account
SET after_hours_text = COALESCE(NULLIF(after_hours_text, ''), 'Thanks for reaching out. Our business hours are daily from 2 PM to 2 AM. We’ll respond as soon as we’re back online.'),
    after_hours_cooldown_minutes = COALESCE(after_hours_cooldown_minutes, 720);
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS pwa_after_hours_push_enabled BOOLEAN DEFAULT FALSE;
