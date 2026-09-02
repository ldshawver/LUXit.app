-- Promotional opt-in — operator send flow delivery tracking. Forward-only, idempotent.
--
-- Additive columns on promotional_optin_solicitation only. Nothing here changes
-- existing contact/consent columns, the campaign resolver, the STOP/START
-- keyword contract, or the "consent is granted only by a contextual inbound
-- YES" invariant. These columns record what happened to the *operator-approved
-- opt-in request SMS* — never consent.
--
--   delivery_status : NULL   -> row recorded but the request SMS was never sent
--                     queued -> handed to Twilio
--                     sent / delivered / failed / undelivered -> Twilio status
--                     blocked -> outbound Twilio disabled (LUXIT_TWILIO_MODE)
--   sent_at         : first successful hand-off to Twilio
--   send_error      : last provider error string, if any
--   last_status_at  : last delivery-status callback applied

ALTER TABLE promotional_optin_solicitation ADD COLUMN IF NOT EXISTS delivery_status VARCHAR(20);
ALTER TABLE promotional_optin_solicitation ADD COLUMN IF NOT EXISTS sent_at         TIMESTAMP WITHOUT TIME ZONE;
ALTER TABLE promotional_optin_solicitation ADD COLUMN IF NOT EXISTS send_error      TEXT;
ALTER TABLE promotional_optin_solicitation ADD COLUMN IF NOT EXISTS last_status_at  TIMESTAMP WITHOUT TIME ZONE;

-- The delivery-status callback resolves a solicitation by its outbound MessageSid.
CREATE INDEX IF NOT EXISTS ix_promo_solicitation_message_sid
    ON promotional_optin_solicitation (solicitation_message_sid);
