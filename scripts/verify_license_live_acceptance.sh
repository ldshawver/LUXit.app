#!/usr/bin/env bash
set -euo pipefail

# LUXit license/billing live acceptance proof runner.
# Required env:
#   DATABASE_URL                     Postgres URL on the VPS
#   LUXIT_BASE_URL                   e.g. https://app.luxit.com or http://127.0.0.1:8001
#   TENANT_ADMIN_COOKIE_FILE         curl cookie jar for a logged-in tenant admin
#   GLOBAL_ADMIN_COOKIE_FILE         curl cookie jar for a logged-in global admin
# Optional env:
#   LUXIT_SERVICE_NAME               default lux-email-bot.service
#   TEST_TWILIO_TO                   default +19165989519
#   TEST_TWILIO_FROM                 default +14155551212
#   TEST_TWILIO_MESSAGE_SID          default SMLICENSELIVEPROOF

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${LUXIT_BASE_URL:?LUXIT_BASE_URL is required}"
: "${TENANT_ADMIN_COOKIE_FILE:?TENANT_ADMIN_COOKIE_FILE is required}"
: "${GLOBAL_ADMIN_COOKIE_FILE:?GLOBAL_ADMIN_COOKIE_FILE is required}"

SERVICE_NAME="${LUXIT_SERVICE_NAME:-lux-email-bot.service}"
TEST_TWILIO_TO="${TEST_TWILIO_TO:-+19165989519}"
TEST_TWILIO_FROM="${TEST_TWILIO_FROM:-+14155551212}"
TEST_TWILIO_MESSAGE_SID="${TEST_TWILIO_MESSAGE_SID:-SMLICENSELIVEPROOF}"
BASE="${LUXIT_BASE_URL%/}"

echo "== 1. Run license billing migration =="
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260621_license_billing_feature_management.sql

echo "== 2. Confirm feature_module rows =="
psql "$DATABASE_URL" -c "SELECT key, is_active FROM feature_module ORDER BY key;"

echo "== 3. Confirm company 1 active phone_pwa_communications license =="
psql "$DATABASE_URL" -c "SELECT company_id, feature_key, status FROM tenant_license WHERE company_id=1 AND feature_key='phone_pwa_communications';"
psql "$DATABASE_URL" -tAc "SELECT CASE WHEN EXISTS (SELECT 1 FROM tenant_license WHERE company_id=1 AND feature_key='phone_pwa_communications' AND status='active') THEN 'PASS' ELSE 'FAIL' END AS company_1_phone_license_active;" | grep -qx PASS

echo "== 4. Tenant admin route smoke =="
curl -fsSI -b "$TENANT_ADMIN_COOKIE_FILE" "$BASE/settings/licenses" | head -n 1
curl -fsSI -b "$TENANT_ADMIN_COOKIE_FILE" "$BASE/settings/billing" | head -n 1

echo "== 5. Global admin route smoke =="
curl -fsSI -b "$GLOBAL_ADMIN_COOKIE_FILE" "$BASE/global-admin/licenses" | head -n 1

echo "== 6. Suspend phone_pwa_communications and confirm /app/inbox blocks =="
python - <<'PY'
from app import app
from services.license_service import suspend_license
with app.app_context():
    lic = suspend_license(1, 'phone_pwa_communications', 'live_acceptance_test', actor_role='live_verification')
    print({'company_id': lic.company_id, 'feature_key': lic.feature_key, 'status': lic.status, 'reason': lic.suspension_reason})
PY
INBOX_STATUS=$(curl -sS -o /tmp/luxit_license_inbox_block.html -w '%{http_code}' -b "$TENANT_ADMIN_COOKIE_FILE" "$BASE/app/inbox")
echo "inbox_status_while_suspended=$INBOX_STATUS"
test "$INBOX_STATUS" = "402"

echo "== 7. Confirm inbound Twilio webhook still logs while suspended =="
curl -fsS -X POST "$BASE/twilio/sms/inbound" \
  --data-urlencode "To=$TEST_TWILIO_TO" \
  --data-urlencode "From=$TEST_TWILIO_FROM" \
  --data-urlencode "Body=license live proof inbound" \
  --data-urlencode "MessageSid=$TEST_TWILIO_MESSAGE_SID" >/tmp/luxit_license_twilio_inbound.xml
psql "$DATABASE_URL" -tAc "SELECT CASE WHEN EXISTS (SELECT 1 FROM twilio_conversation WHERE company_id=1 AND from_number='$TEST_TWILIO_FROM') THEN 'PASS' ELSE 'FAIL' END AS inbound_logged_while_suspended;" | grep -qx PASS

echo "== 8. Simulate Stripe invoice.payment_failed and invoice.payment_succeeded =="
python - <<'PY'
from app import app
from services.license_service import sync_license_from_stripe_event
with app.app_context():
    failed = {
        'id': 'evt_live_license_failed',
        'type': 'invoice.payment_failed',
        'data': {'object': {'id': 'in_live_license_failed', 'customer': 'cus_live_license_test', 'subscription': 'sub_live_license_test', 'status': 'open', 'amount_due': 1234, 'amount_paid': 0, 'currency': 'usd'}}
    }
    paid = {
        'id': 'evt_live_license_paid',
        'type': 'invoice.payment_succeeded',
        'data': {'object': {'id': 'in_live_license_failed', 'customer': 'cus_live_license_test', 'subscription': 'sub_live_license_test', 'status': 'paid', 'amount_due': 1234, 'amount_paid': 1234, 'currency': 'usd'}}
    }
    print(sync_license_from_stripe_event(failed))
    print(sync_license_from_stripe_event(paid))
PY

echo "== 9. Reactivate license and confirm /app/inbox works again =="
python - <<'PY'
from app import app
from services.license_service import reactivate_license
with app.app_context():
    lic = reactivate_license(1, 'phone_pwa_communications', actor_role='live_verification')
    print({'company_id': lic.company_id, 'feature_key': lic.feature_key, 'status': lic.status})
PY
INBOX_ACTIVE_STATUS=$(curl -sS -o /tmp/luxit_license_inbox_active.html -w '%{http_code}' -b "$TENANT_ADMIN_COOKIE_FILE" "$BASE/app/inbox")
echo "inbox_status_after_reactivation=$INBOX_ACTIVE_STATUS"
test "$INBOX_ACTIVE_STATUS" = "200"

echo "== 10. Confirm audit/event logs exist =="
psql "$DATABASE_URL" -c "SELECT event_type, old_status, new_status, created_at FROM license_event_log WHERE company_id=1 ORDER BY created_at DESC LIMIT 20;"
psql "$DATABASE_URL" -tAc "SELECT CASE WHEN EXISTS (SELECT 1 FROM license_event_log WHERE company_id=1 AND event_type IN ('license_suspended','license_reactivated','stripe_payment_failed','stripe_payment_succeeded')) THEN 'PASS' ELSE 'FAIL' END AS license_events_created;" | grep -qx PASS

echo "== 11. journalctl regression scan =="
journalctl -u "$SERVICE_NAME" --since '30 minutes ago' --no-pager | egrep -i '500|UndefinedColumn|BuildError|ProgrammingError|InFailedSqlTransaction' && {
  echo 'FAIL: runtime error signature found in journalctl' >&2
  exit 1
} || echo 'PASS: no matching runtime error signatures found'

echo "LIVE LICENSE ACCEPTANCE: PASS"
