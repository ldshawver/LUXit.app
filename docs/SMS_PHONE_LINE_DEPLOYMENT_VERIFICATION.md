# SMS Phone-Line Campaign Deployment Verification

## Forward migration

The production workflow `.github/workflows/push-to-production.yml` runs every `migrations/*.sql` file with `psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"` when `psql` and the migrations directory are present. Confirm that workflow is still active before deploy.

Run the migration manually on staging/production with PostgreSQL error-stop enabled if you need to verify outside the workflow:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260619_sms_phone_line_campaign_completion.sql
```

The migration is idempotent: it uses `ADD COLUMN IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`, does not drop data, and can be re-run safely during production deploy retries.

## Executable PWA deployment steps

Run these commands on the live VPS before acceptance. They convert the prior “live verification still required” notes into concrete deployment actions and checks.

Set shell variables for the session:

```bash
cd /var/www/LUXit.app
export APP_URL="https://luxit.app"
export LOCAL_APP_URL="http://127.0.0.1:8001"
export SERVICE="luxit"
export REMINDER_SERVICE="luxit-pwa-unread-reminders"
```

Apply the PWA alerts/greetings migration directly if the deploy workflow has not already applied it:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260621_pwa_alerts_greetings.sql
```

Validate VAPID/Web Push environment values. Expected: either `VAPID_OK=1` or the exact missing variable names are printed so the server no longer only says “not enabled” without setup detail.

```bash
python - <<'PY'
import os
required = ["VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"]
missing = [k for k in required if not os.environ.get(k)]
print("VAPID_OK=1" if not missing else "VAPID_MISSING=" + ",".join(missing))
raise SystemExit(1 if missing else 0)
PY
```

Install a systemd timer to run unread-message reminders every minute. This is preferred over ad-hoc execution because the reminder job must repeat until a conversation is read, replied to, or auto-replied/resolved.

```bash
sudo tee /etc/systemd/system/${REMINDER_SERVICE}.service >/dev/null <<'EOF'
[Unit]
Description=LUXit PWA unread-message reminder run
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/var/www/LUXit.app
EnvironmentFile=-/etc/luxit.env
ExecStart=/usr/bin/env python scripts/run_pwa_unread_reminders.py
User=www-data
Group=www-data
EOF

sudo tee /etc/systemd/system/${REMINDER_SERVICE}.timer >/dev/null <<'EOF'
[Unit]
Description=Run LUXit PWA unread-message reminders every minute

[Timer]
OnBootSec=60
OnUnitActiveSec=60
AccuracySec=5
Unit=luxit-pwa-unread-reminders.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ${REMINDER_SERVICE}.timer
systemctl list-timers --all | grep "$REMINDER_SERVICE"
```

If the VPS uses cron instead of systemd timers, install this equivalent entry:

```bash
(crontab -l 2>/dev/null | grep -v 'scripts/run_pwa_unread_reminders.py'; echo '* * * * * cd /var/www/LUXit.app && /usr/bin/env python scripts/run_pwa_unread_reminders.py >> /var/log/luxit-pwa-unread-reminders.log 2>&1') | crontab -
```

Restart the app and email/worker service after migration/env/timer changes:

```bash
sudo systemctl restart luxit
sudo systemctl restart lux-email-bot
sudo systemctl is-active luxit lux-email-bot
```

Run the deployment audit script to prove the pushed code is the code running live, migrations ran, rollback migrations were excluded, `lux-email-bot.service` was restarted on port `8001`, and the PWA DOM/CSS/service-worker cache-busting changes are visible after refresh/reinstall:

```bash
cd /var/www/LUXit.app
export REPO_DIR="/var/www/LUXit.app"
export REMOTE="origin"
export BRANCH="main"
export SERVICE="lux-email-bot.service"
export PORT="8001"
export BASE_URL="http://127.0.0.1:8001"
export COOKIE_FILE="/tmp/luxit.cookies"
scripts/audit_vps_deployment.sh
```

Expected result: the script prints matching `HEAD=` and `origin/main=` SHAs, applies only non-rollback `migrations/*.sql`, confirms `lux-email-bot.service` is active/listening on `:8001`, finds `data-pwa-version`, `manifest.json?v=`, `sw.js?v=`, exact VAPID missing-setting UI text, larger icon / hidden-label bottom-nav CSS, service-worker `SW_VERSION`, and exits with `DEPLOY_AUDIT_OK=1`.

## Schema drift verification

After migration, verify the tables used by SMS, phone routing, campaign calendar, and PWA history still expose the expected columns:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -c "\d twilio_phone_number" \
  -c "\d twilio_account" \
  -c "\d sms_campaign" \
  -c "\d sms_recipient" \
  -c "\d sms_template" \
  -c "\d twilio_conversation" \
  -c "\d twilio_message" \
  -c "\d calendar_event" \
  -c "\di ix_twilio_phone_number_company_campaign_sender" \
  -c "\di ix_sms_campaign_company_status_scheduled"
```

Expected evidence:

- `twilio_phone_number` includes `status_callback_webhook_url`, `number_auto_reply_text`, `campaign_sender_enabled`, `campaign_default_batch_size`, `campaign_send_rate_per_minute`, and `allow_global_fallback`.
- `sms_campaign` includes `media_urls`, `batch_size`, `send_rate_per_minute`, `canceled_at`, and `archived_at`.
- `sms_recipient`, `twilio_conversation`, and `twilio_message` remain present so existing PWA conversation history is preserved.
- Both new indexes are present.

## Route smoke checks

Authenticated browser checks:

```bash
curl -I https://<host>/sms/create
curl -I https://<host>/app/sms-campaigns
curl -I https://<host>/settings/phone-lines
curl -I https://<host>/admin/phone-lines
curl -I https://<host>/admin/communications
curl -I https://<host>/sms-dashboard
```

Expected evidence:

- `/sms/create`, `/app/sms-campaigns`, `/settings/phone-lines`, `/admin/phone-lines`, and `/admin/communications` return 200 for an authorized admin.
- `/sms-dashboard` redirects to a working SMS campaigns route.
- No `UndefinedColumn`, `BuildError`, or 500 appears in application logs.

## Conditional acceptance gate

This change is not production-certified from the local Codex environment alone. Treat merge/deploy as appropriate only after the live LUXit VPS passes the checks below and `journalctl` shows no `UndefinedColumn`, `ProgrammingError`, `BuildError`, or permission errors for the touched routes.

## Functional staging checks

1. Open **Phone Lines & Webhooks** and verify every number shows exact inbound SMS, inbound voice, and status callback webhook fields.
2. Confirm each line clearly shows whether it is line-specific or has global/API fallback enabled.
3. Send inbound SMS and inbound call traffic to each managed number and verify audit logs record the expected `company_id`, phone-number ID, and settings source.
4. Create a scheduled SMS campaign with a permitted sender number, edit message/media/sender/date/time before send, duplicate it, test-send it, cancel it, and archive it.
5. Upload the canonical CSV/XLSX contact template and confirm preview maps text opt-in, email opt-in, do-not-market, SMS opt-out, email opt-out, tags, notes, and duplicate status.
6. Confirm opted-out contacts are skipped and Twilio failures mark recipients failed instead of sent.
7. Confirm scheduled SMS campaigns appear on the marketing calendar, analytics, and AI SMS campaign context.
8. Confirm a staff/mobile-inbox user can open `/app/inbox` without manage/campaign permissions.
9. Confirm a non-admin cannot send from an unassigned number, while an admin/owner can send from permitted company numbers.
10. Confirm PWA inbox history remains visible for all permitted numbers after browser/PWA reopen, including read conversations in `filter=all`, and that no user-only filter hides shared-number history.

## Live log checks

```bash
journalctl -u luxit --since "30 minutes ago" --no-pager | egrep -i "UndefinedColumn|ProgrammingError|BuildError|permission|Traceback" || true
```

Expected evidence: no new errors tied to `/sms/create`, `/app/sms-campaigns`, `/twilio/comms`, `/app/inbox`, phone-line settings, campaign send, or contact import preview.

## Rollback notes

The preferred rollback is code rollback only because the added columns are additive and preserve data. If a database rollback is explicitly required after exporting/snapshotting data, drop only the migration-owned indexes and columns:

```sql
DROP INDEX IF EXISTS ix_twilio_phone_number_company_campaign_sender;
DROP INDEX IF EXISTS ix_sms_campaign_company_status_scheduled;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS status_callback_webhook_url;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS number_auto_reply_text;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS campaign_sender_enabled;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS campaign_default_batch_size;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS campaign_send_rate_per_minute;
ALTER TABLE twilio_phone_number DROP COLUMN IF EXISTS allow_global_fallback;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS media_urls;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS batch_size;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS send_rate_per_minute;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS canceled_at;
ALTER TABLE sms_campaign DROP COLUMN IF EXISTS archived_at;
```

Do not run the SQL rollback on production unless product owners accept losing values stored in the new settings fields.

## Voice inbound emergency verification

Use these commands on the live VPS after applying migrations and restarting the service. They verify that `/twilio/voice/inbound` returns TwiML instead of 500 and that the first exception is logged before rollback-safe fallback handling.

During-hours forward check for `+19165989519`:

```bash
curl -i -X POST http://127.0.0.1:8001/twilio/voice/inbound \
  -d "To=%2B19165989519" \
  -d "From=%2B14155551212" \
  -d "CallSid=TEST_FORWARD_DEBUG_005" \
  -d "Direction=inbound"
```

Expected result: HTTP 200 `text/xml` TwiML containing `<Dial` and `<Number>+12792860000</Number>`.

Ring-PWA check for `+18302591310`:

```bash
curl -i -X POST http://127.0.0.1:8001/twilio/voice/inbound \
  -d "To=%2B18302591310" \
  -d "From=%2B14155551212" \
  -d "CallSid=TEST_RING_PWA_DEBUG_001" \
  -d "Direction=inbound"
```

Expected result: HTTP 200 `text/xml` TwiML containing `<Response>` and either `<Client>` for ring-PWA routing or safe voicemail TwiML if the line is outside business hours.

Voice error log check:

```bash
journalctl -u luxit --since "15 minutes ago" --no-pager | egrep -i "FIRST_EXCEPTION voice inbound|InFailedSqlTransaction|UndefinedColumn|ProgrammingError|Traceback" || true
```

Expected result: no `InFailedSqlTransaction`; if a voice exception occurs, the first log line starts with `FIRST_EXCEPTION voice inbound failed` and includes `CallSid`, `To`, `From`, `company_id`, `phone_number_id`, route decision, and forwarding target.

Status: `/twilio/voice/inbound` 500 handling is fixed by this PR if the during-hours forward, ring-PWA, after-hours voicemail, and log checks above pass on the VPS. If any command returns HTTP 500 or logs `InFailedSqlTransaction`, that is a merge/deploy blocker for this PR rather than a separate follow-up.

## Exact VPS PWA verification commands

Authenticate once in a browser or with the login endpoint and save the authorized session cookie at `/tmp/luxit.cookies`. Then run these commands from the VPS. Replace `<phone_number_id>`, `<call_id>`, and `<greeting_id>` with live IDs assigned to the signed-in user.

For the final acceptance run, use the executable verifier so all still-required items are checked in one pass:

```bash
cd /var/www/LUXit.app
export LUXIT_BASE_URL="http://127.0.0.1:8001"
export LUXIT_COOKIE_FILE="/tmp/luxit.cookies"
export LUXIT_VERIFY_VOICE_TO="+19165989519"
export LUXIT_VERIFY_VOICE_FROM="+14155551212"
export LUXIT_CALL_ID="<call_id_with_voicemail>"
export LUXIT_PHONE_NUMBER_ID="<assigned_phone_number_id>"
export LUXIT_CONTACT_PHONE="+14155551212"
export LUXIT_EXPECTED_CONTACT_NAME="John Smith"
python scripts/verify_pwa_live_acceptance.py --write-tests --run-live-reminder
```

Expected result: `SUMMARY failed=0`. Any failure means the VPS is not accepted yet. Skips are allowed only when the required live fixture does not exist yet; create the fixture and rerun before marking the item verified.

The executable verifier covers the remaining acceptance list:

1. `/twilio/voice/inbound` returns HTTP 200 TwiML instead of 500.
2. Voicemail audio streams through `/api/calls/<call_id>/voicemail/audio` with no Twilio login page.
3. Push status is configured or lists exact missing VAPID keys instead of vague “not enabled on server.”
4. Alert preferences save sound/vibration settings used by in-app alerts.
5. Unread reminders support dry-run, live run, and one-minute scheduler verification.
6. Existing live conversations display Google/CRM contact names after migration/backfill.
7. PWA theme files use shared `--pwa-*` variables and no hardcoded purple controls.
8. Phone-line settings persistence is verified by the per-number save/reload section below; run it on the same VPS before acceptance.

Push setup endpoint/status:

```bash
curl -fsS -b /tmp/luxit.cookies "$APP_URL/api/pwa/push/status" | jq .
```

Expected result: JSON contains `"configured": true`; if false, `missing` lists the exact absent VAPID keys (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, and/or `VAPID_SUBJECT`).

Alert preference save endpoint:

```bash
curl -fsS -b /tmp/luxit.cookies -H 'Content-Type: application/json' \
  -X PATCH "$APP_URL/api/pwa/preferences" \
  -d '{"text_alerts":true,"call_alerts":true,"voicemail_alerts":true,"unread_reminder_alerts":true,"sound_enabled":true,"vibration_enabled":true,"business_hours_only":true,"quiet_hours_start":"22:00","quiet_hours_end":"07:00","unread_reminder_minutes":1}' | jq .
```

Expected result: `"success": true` and returned preferences reflect the submitted values.

Voicemail audio proxy test:

```bash
curl -fL -b /tmp/luxit.cookies -o /tmp/luxit-voicemail-test.audio \
  "$APP_URL/api/calls/<call_id>/voicemail/audio"
file /tmp/luxit-voicemail-test.audio
test -s /tmp/luxit-voicemail-test.audio
```

Expected result: HTTP 200 playable audio downloaded through LUXit; no browser or curl redirect to a Twilio sign-in page.

Greeting list/create/activate test:

```bash
curl -fsS -b /tmp/luxit.cookies "$APP_URL/api/phone/numbers/<phone_number_id>/greetings" | jq .

GREETING_ID="$(curl -fsS -b /tmp/luxit.cookies -H 'Content-Type: application/json' \
  -X POST "$APP_URL/api/phone/numbers/<phone_number_id>/greetings" \
  -d '{"name":"VPS verification greeting","greeting_type":"standard","text_body":"Thank you for calling LUXit. Please leave a message.","applies_to":"voicemail_default","is_active":false}' \
  | jq -r '.greeting.id')"

curl -fsS -b /tmp/luxit.cookies -X POST "$APP_URL/api/phone/greetings/${GREETING_ID}/activate" | jq .
```

Expected result: greeting create returns HTTP 201 with a greeting scoped to the selected `phone_number_id`; activate returns `"success": true` and that greeting is active only for that number/scope.

Unread reminder dry-run/live run:

```bash
python scripts/run_pwa_unread_reminders.py --dry-run | jq .
python scripts/run_pwa_unread_reminders.py | jq .
curl -fsS -b /tmp/luxit.cookies -X POST "$APP_URL/api/pwa/reminders/unread/run?dry_run=1" | jq .
curl -fsS -b /tmp/luxit.cookies -X POST "$APP_URL/api/pwa/reminders/unread/run" | jq .
systemctl status luxit-pwa-unread-reminders.timer --no-pager
journalctl -u luxit-pwa-unread-reminders.service --since "10 minutes ago" --no-pager
```

Expected result: dry-runs return JSON with `dry_run=true` and a `would_create` count without writing rows; live runs return a `created` count. Repeated timer runs create at most one reminder per unresolved unread conversation per minute and stop after the conversation is read, replied to, or auto-replied/resolved.

Final journal check:

```bash
journalctl -u luxit --since "30 minutes ago" --no-pager \
  | egrep -i " 500 |UndefinedColumn|InFailedSqlTransaction|ProgrammingError|Traceback|Twilio auth prompt|not enabled on server" || true
```

Expected result: no new route 500s, `UndefinedColumn`, `InFailedSqlTransaction`, Twilio-login/voicemail auth prompt, or generic push “not enabled on server” failures after the checks above.

## Browser alert fallback note

iOS and some browsers restrict background notification sound. Acceptance for those browsers is: push notification where supported, vibration where supported, badge/unread count where supported, and in-app sound while the PWA is open and the user has sound enabled. The PWA must show exact missing VAPID/server setup details when push cannot be enabled; it must not silently fail or show only a vague “server not enabled” message.

## PWA communications live verification gate

Run these checks on the live VPS after migrations and before declaring the PWA production-ready:

1. Contact names
   - Sync Google Contacts for a test user.
   - Send an inbound SMS from a synced contact such as John Smith.
   - Verify `/app/inbox` and `GET /api/inbox/conversations?filter=all` show `display_name=John Smith` with the phone number only as the subtitle.

2. Voicemail playback
   - Trigger a voicemail on an assigned number.
   - Open `/app/calls`, select the Voicemails tab, and click Play Voicemail.
   - Expected: audio streams from `/api/calls/<call_id>/voicemail/audio` with no Twilio email/password prompt.
   - Verify an unassigned user cannot fetch the same voicemail URL.

3. Push and in-app alerts
   - Confirm `GET /api/pwa/push/status` returns `configured=true`; if false, set the listed VAPID env keys.
   - Trigger inbound SMS, incoming call, missed call, and voicemail.
   - Expected: permitted users receive notification records, Web Push where supported, in-app SSE update, sound when PWA is open and sound is enabled, vibration where supported, and click-through to the correct thread/call.

4. Unified styling and palette
   - Change the PWA palette picker.
   - Expected: message cards, call cards, voicemail controls, tabs, badges, buttons, and bottom navigation all update from the same `--pwa-*` theme variables.

5. Logs
   - Run `journalctl -u luxit -n 300 --no-pager | egrep -i '500|UndefinedColumn|ProgrammingError|Twilio auth prompt|Traceback'`.
   - Expected: no new errors after the above SMS/call/voicemail/push checks.

## Existing PWA conversation contact-name backfill

Use this audit/fix workflow on the live VPS when existing SMS threads still show phone numbers after Google Contacts sync.

1. Confirm Google/CRM contact cache fields for the known sender number:

```sql
SELECT id, company_id, name, first_name, last_name, phone, normalized_phone, source
FROM contact
WHERE company_id = <company_id>
  AND (normalized_phone = '+14155551212' OR regexp_replace(phone, '\\D', '', 'g') IN ('14155551212', '4155551212'));
```

2. Check the existing conversation before backfill:

```sql
SELECT id, company_id, contact_id, from_number, to_number, contact_name, contact_source
FROM twilio_conversation
WHERE company_id = <company_id>
  AND regexp_replace(from_number, '\\D', '', 'g') IN ('14155551212', '4155551212');
```

3. Run a dry-run, then commit the backfill:

```bash
python scripts/backfill_conversation_contact_names.py --company-id <company_id> --dry-run
python scripts/backfill_conversation_contact_names.py --company-id <company_id>
```

4. Verify conversation rows now map to the contact:

```sql
SELECT c.id, c.from_number, c.contact_id, c.contact_name, ct.name, ct.normalized_phone, ct.source
FROM twilio_conversation c
LEFT JOIN contact ct ON ct.id = c.contact_id
WHERE c.company_id = <company_id>
  AND regexp_replace(c.from_number, '\\D', '', 'g') IN ('14155551212', '4155551212');
```

5. Verify the API and browser:

```bash
curl -sS -b /tmp/luxit.cookies 'https://<host>/api/inbox/conversations?filter=all' | jq '.conversations[] | select(.from_number=="+14155551212") | {contact_name, display_name, from_number}'
```

Expected result: `contact_name` and `display_name` are the synced/CRM name, and the phone number remains available as `from_number` for the PWA subtitle. Refresh/reopen `/app/inbox` and confirm the conversation list and thread header show the name first.

## PWA communications completion verification

After deploying the PWA communications fixes, run these live checks in addition to the SMS/phone-line checks above:

1. Push setup and preferences
   - Open `/app/inbox`, then PWA Settings → Push Notifications.
   - `GET /api/pwa/push/status` must return `configured=true`; if not, the response must list missing `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, and/or `VAPID_SUBJECT`.
   - Update alert preferences and confirm the API persists text, call, voicemail, unread reminder, sound, vibration, business-hours-only, quiet-hours, and repeat-minute settings.

2. Alert/reminder behavior
   - Send an inbound text during the assigned phone number's business hours.
   - Expected: permitted users receive notification rows, Web Push where supported, in-app SSE update, in-app sound when open, vibration where supported, and badge/unread count.
   - Leave the conversation unread and run `python scripts/run_pwa_unread_reminders.py` once per minute via cron/systemd timer. Expected: one unread reminder per minute until the conversation is read, replied to, or auto-replied/resolved; no duplicate reminder storm inside the same minute.

3. Calls and voicemail
   - Trigger incoming call, missed call, and voicemail. Expected: alerts respect per-number permissions and the Calls page keeps loading even if the Twilio Voice SDK is unavailable.
   - Click Play Voicemail. Expected: `/api/calls/<call_id>/voicemail/audio` streams playable audio inside LUXit with no Twilio sign-in page.

4. Theme/navigation
   - Change the PWA palette. Expected: Messages, Recents/Calls, Voicemail, Contacts, Keypad, Favorites, and Settings use the shared `--pwa-*` theme variables with no hardcoded purple button backgrounds.
   - Confirm the calls bottom navigation is comfortably inset from safe areas and shows Favorites, Recents, Contacts, Keypad, and Settings.

5. Greeting management
   - In Calls → Settings, create standard, uploaded, recorded, typed/text-to-speech, and AI voice greeting entries as applicable.
   - Verify greetings are scoped by `phone_number_id`, can be previewed/activated, and active greetings are separate for business-hours, after-hours, and default voicemail scopes.

## Per-number auto-reply and voicemail greeting save verification

When editing a phone line in `/settings/phone-lines` or `/admin/phone-lines`, verify these fields save and reload for that same number only:

1. Update **Business-hours Auto Reply for this number**, **After-hours Auto Reply for this number**, **Missed-call Text for this number**, and **Voicemail Greeting for this number**.
2. Click **Save phone-line settings**, then reload the page and confirm the edited text remains in the same fields.
3. Send an inbound SMS to the number during open business hours. Expected: if auto replies are enabled and no higher-priority auto-reply rule sends first, the number-specific business-hours auto reply is sent.
4. Send an inbound SMS to the number after hours. Expected: if after-hours SMS is enabled, the number-specific after-hours auto reply is sent.
5. Call the number and route to voicemail. Expected: the number-specific voicemail greeting is used; changing one number must not change another number's greeting.

## Tenant license billing and feature management live verification

Run these commands after deploying the license/billing PR to the VPS. They are intentionally executable and should be captured in the deployment log.

### 1. Apply the license migration

```bash
cd /opt/LUXit.app
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260621_license_billing_feature_management.sql
```

Expected result: the migration completes without `UndefinedColumn`, duplicate-table, or rollback errors.

### 2. Confirm seeded feature modules and default phone license

```bash
psql "$DATABASE_URL" -c "SELECT key, is_active FROM feature_module ORDER BY key;"
psql "$DATABASE_URL" -c "SELECT company_id, feature_key, status FROM tenant_license WHERE feature_key='phone_pwa_communications' ORDER BY company_id;"
```

Expected result: core modules exist, and company `1` has an `active` `phone_pwa_communications` license so the existing deployment keeps phone/PWA access.

### 3. Verify tenant and global pages after login

```bash
curl -I -b /tmp/luxit-admin.cookies https://<host>/settings/licenses
curl -I -b /tmp/luxit-admin.cookies https://<host>/settings/billing
curl -I -b /tmp/luxit-admin.cookies https://<host>/settings/billing/statements
curl -I -b /tmp/luxit-global-admin.cookies https://<host>/global-admin/licenses
curl -I -b /tmp/luxit-global-admin.cookies https://<host>/global-admin/billing
curl -I -b /tmp/luxit-global-admin.cookies https://<host>/global-admin/features
```

Expected result: tenant admin routes return `200`; global routes return `200` only for global admins; regular users receive `403` for billing/license admin routes.

### 4. Simulate failed payment and suspension flow

```bash
psql "$DATABASE_URL" -c "UPDATE tenant_license SET status='past_due', renews_at=now() - interval '10 days', auto_disable_enabled=true, grace_period_days=3 WHERE company_id=1 AND feature_key='phone_pwa_communications';"
python - <<'PY'
from app import app
from extensions import db
from services.license_service import auto_suspend_past_due
with app.app_context():
    print(auto_suspend_past_due())
    db.session.commit()
PY
psql "$DATABASE_URL" -c "SELECT status, suspension_reason FROM tenant_license WHERE company_id=1 AND feature_key='phone_pwa_communications';"
```

Expected result: the license changes to `suspended` with `suspension_reason='non_payment'`, and `/app/inbox` / `/app/calls` are blocked while `/settings/billing` remains reachable.

### 5. Reactivate and verify phone/PWA access returns

```bash
python - <<'PY'
from app import app
from extensions import db
from services.license_service import reactivate_license
with app.app_context():
    print(reactivate_license(1, 'phone_pwa_communications'))
    db.session.commit()
PY
curl -I -b /tmp/luxit-admin.cookies https://<host>/app/inbox
curl -I -b /tmp/luxit-admin.cookies https://<host>/app/calls
```

Expected result: license status returns to `active`, audit events exist in `license_event_log`, and phone/PWA routes work for authorized users.

### 6. Stripe webhook safety checks

```bash
curl -i -X POST https://<host>/api/stripe/webhook \
  -H 'Content-Type: application/json' \
  -H 'Stripe-Signature: invalid' \
  --data '{"type":"invoice.payment_failed","data":{"object":{"id":"in_invalid"}}}'
```

Expected result: invalid signatures return `400`; no Stripe secret key or full payment details are exposed in responses or logs. Use the Stripe CLI for signed live/test events when validating `invoice.payment_failed` and `invoice.payment_succeeded` state sync.

### 7. Confirm no runtime regressions

```bash
journalctl -u lux-email-bot.service --since '30 minutes ago' --no-pager | egrep -i '500|UndefinedColumn|BuildError|ProgrammingError|permission leakage|stripe secret|InFailedSqlTransaction' || true
```

Expected result: no matching runtime errors after exercising license pages, Stripe webhook sync, suspension/reactivation, `/app/inbox`, `/app/calls`, `/settings/phone-lines`, `/app/sms-campaigns`, and `/sms/create`.

## License billing live proof script

For final production acceptance, run the executable proof script on the VPS after logging in once as tenant admin and global admin and saving their curl cookie jars:

```bash
cd /opt/LUXit.app
export LUXIT_BASE_URL="https://<host>"
export TENANT_ADMIN_COOKIE_FILE="/tmp/luxit-admin.cookies"
export GLOBAL_ADMIN_COOKIE_FILE="/tmp/luxit-global-admin.cookies"
export LUXIT_SERVICE_NAME="lux-email-bot.service"
./scripts/verify_license_live_acceptance.sh | tee /tmp/luxit-license-live-acceptance.log
```

The script performs the required production proof steps: runs `migrations/20260621_license_billing_feature_management.sql`, confirms `feature_module` rows, confirms company `1` has an active `phone_pwa_communications` license, verifies `/settings/licenses`, `/settings/billing`, and `/global-admin/licenses`, suspends the license and confirms `/app/inbox` returns `402`, posts an inbound Twilio SMS while suspended and verifies it still logs, simulates Stripe `invoice.payment_failed` and `invoice.payment_succeeded` through the license sync service, reactivates the license and confirms `/app/inbox` returns `200`, confirms audit/event logs exist, and scans `journalctl` for `500`, `UndefinedColumn`, `BuildError`, `ProgrammingError`, and `InFailedSqlTransaction`.

Acceptance remains blocked until `/tmp/luxit-license-live-acceptance.log` ends with `LIVE LICENSE ACCEPTANCE: PASS` on the live VPS.
