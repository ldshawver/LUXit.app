# Marketing Hub Deployment Notes

## Supported runtime
Use Python 3.11 or 3.12. Python 3.14 is not supported for this repo until all pinned native dependencies publish compatible wheels.

## Apply migration

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/marketing_hub_sms_20260615.sql
```

The migration is idempotent and uses `IF NOT EXISTS` for tables, columns, and indexes.

## Roll back migration

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/marketing_hub_sms_20260615_rollback.sql
```

Rollback drops Marketing Hub keyword/auto-reply/audit data and migration-named indexes. It intentionally does not drop shared columns on existing tables because some deployments may already have those columns from startup backfills or manual hotfixes. Export data before rollback if needed.

## Required SMS env vars / tenant config
Prefer tenant `TwilioAccount` records. Env fallback requires:

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_PHONE_NUMBER` or `TWILIO_FROM_NUMBER`
- `TWILIO_WEBHOOK_PUBLIC_URL` when generated callback URLs need an explicit public base URL

## Twilio webhook URLs
Configure Twilio Messaging Service or phone number webhooks:

- Inbound SMS: `https://<your-domain>/twilio/sms/inbound`
- Delivery status callback: `https://<your-domain>/twilio/sms/status`

## Post-deploy smoke checklist

1. Open `/campaigns`, `/sms/campaigns`, and `/social-media` as a tenant user.
2. Create an SMS campaign with a test consented contact.
3. Preview recipients and confirm opted-out contacts are excluded.
4. Send to a Twilio test or live number.
5. Confirm delivery callback updates `sms_recipient.status`.
6. Reply to the SMS and confirm campaign recipient status becomes `replied`.
7. Reply `STOP` and confirm contact opt-out, recipient opt-out, and audit log rows.
8. Reply `START` only for numbers where resubscribe is legally appropriate.
9. Create keyword `VIP`, reply `VIP`, and confirm tag/segment/audit plus auto-reply.
10. Confirm `X-Twilio-Signature` strict validation succeeds for inbound and delivery callbacks.
11. Confirm duplicate campaign sends return a conflict and do not create second sends.
12. Confirm disconnected social state displays when platform credentials are missing.
