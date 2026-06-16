# Marketing Hub Production Evidence Matrix

Status: **not production-ready** until staging PostgreSQL and live/Twilio test-number evidence is attached to the release ticket.

## 1. Concurrent send test

Implementation controls:
- `/api/marketing/sms-campaigns/:id/send` locks the campaign row with `SELECT ... FOR UPDATE`.
- Recipient batch selection uses `FOR UPDATE SKIP LOCKED` and a bounded `batch_size` of 1–100.
- `sms_recipient` has uniqueness on `(campaign_id, contact_id)` and on non-null `provider_message_sid` to prevent duplicate materialization/provider attribution.

Required staging proof command:

```bash
TEST_DATABASE_URL="$STAGING_DATABASE_URL" \
python -m pytest -q tests/test_marketing_hub_regression.py::test_ten_simultaneous_send_requests_no_duplicate_recipient_sends
```

The default SQLite test run skips this case because SQLite and Flask's in-process test client do not provide production row-level locking semantics.

## 2. Twilio replay protection

Inbound callbacks use `TwilioMessage.twilio_sid` as the idempotency key; the model has a unique `twilio_sid`. Duplicate inbound `MessageSid` rows are skipped before keyword/compliance side effects run.

Delivery callbacks use `SMSRecipient.provider_message_sid` as the idempotency key. Duplicate status callbacks with the same status/error payload return without writing a second delivery audit record.

Database idempotency/index keys:
- `twilio_message.twilio_sid` unique model constraint.
- `uq_sms_recipient_provider_message_sid` / `ux_sms_recipient_provider_message_sid_not_null`.
- `uq_sms_recipient_campaign_contact` / `ux_sms_recipient_campaign_contact`.

## 3. STOP/START/HELP compliance

Automated regression coverage demonstrates:
- `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, and `QUIT` unsubscribe the contact and tag `sms_opt_out`.
- `START` restores subscription state and consent tagging for test-client behavior.
- `HELP` returns a help response with STOP language.

Live/legal resubscribe review is still required before enabling START/UNSTOP in production jurisdictions.

## 4. Campaign statistics reconciliation

Regression coverage verifies mixed recipient statuses reconcile independently for:
- `delivered`
- `failed`
- `opted_out`
- `queued`
- `sent`
- `recipients_selected`

## 5. Tenant isolation

Regression coverage verifies tenant A cannot:
- Materialize tenant B contacts into tenant A campaign previews/recipient rows.
- Mutate tenant B recipient delivery status with tenant A's Twilio signature/token.
- Read tenant B marketing audit rows through `/api/marketing/audit-logs`.

## 6. Database integrity

Migration-added integrity:
- Primary keys on new `sms_keyword_rule`, `sms_auto_reply_rule`, and `marketing_audit_log` tables.
- Unique recipient/provider idempotency indexes: `ux_sms_recipient_provider_message_sid_not_null`, `ux_sms_recipient_campaign_contact`.
- Foreign keys added as `NOT VALID` to avoid failing deployment on legacy/backfilled rows; validate them after cleanup.
- Check constraints for SMS campaign status, SMS recipient status, and keyword match type.
- Append-only trigger for `marketing_audit_log` prevents `UPDATE` and `DELETE` at the database layer.

Post-deploy validation command:

```bash
psql "$DATABASE_URL" -P pager=off -c "SELECT conname, contype, convalidated FROM pg_constraint WHERE conname LIKE 'fk_sms_%' OR conname LIKE 'ck_sms_%' OR conname LIKE 'fk_marketing_%' ORDER BY conname;"
```

## 7. Audit logging

`marketing_audit_log` is append-only in PostgreSQL after the migration trigger is installed. The Marketing API exposes a tenant-scoped read endpoint and does not expose update/delete audit endpoints.

## 8. CI readiness

Marketing regression gates run cleanly. The full repository suite still has pre-existing collection blockers outside Marketing Hub:
- `tests/test_analytics_exports.py`: duplicate SQLAlchemy `user` table registration between model packages.
- `tests/test_market_intelligence.py`: missing `CompetitorContent` import from `models`.

Until those suites are fixed, deployment gates should explicitly include the Marketing Hub regression suite, py_compile, ruff on changed Marketing files, PostgreSQL migration apply/rollback/reapply, and live Twilio smoke tests.
