# Phase 5 Runbook (v3.8.5) — Owned Channel Analytics (Email/SMS)

> Purpose: Validate owned-channel analytics (email/SMS) ingestion, reporting, and attribution windows on the VPS.
> If your VPS requires elevated permissions, prepend the commands with `sudo`.

## 1) Pre-flight (Service + Env)
```bash
cd /opt/luxit
source /opt/luxit/venv/bin/activate
python -m py_compile wsgi.py
systemctl status lux.service --no-pager -l
```

## 2) Health + Auth Smoke Check
```bash
curl -sSf http://127.0.0.1:5000/ >/dev/null
curl -sSf http://127.0.0.1:5000/auth/login >/dev/null
```

## 3) Email/SMS Event Ingestion (Sample)
> Replace `company_id` with a valid company in your database.
```bash
# email_send
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "email_send",
    "consent": true,
    "session_id": "sess_phase5_email",
    "properties": {
      "campaign_id": "camp_123",
      "provider": "smtp",
      "recipient_hash": "hash_abc"
    }
  }'

# sms_send
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "sms_send",
    "consent": true,
    "session_id": "sess_phase5_sms",
    "properties": {
      "campaign_id": "sms_456",
      "provider": "twilio",
      "recipient_hash": "hash_def"
    }
  }'
```

## 4) Channel Reports + Export
```bash
curl -sSf "http://127.0.0.1:5000/analytics/report?company_id=1" >/dev/null
curl -sSf "http://127.0.0.1:5000/analytics/report/export/csv?company_id=1" -o /tmp/analytics_phase5.csv
```

## 5) Logs (Debugging)
```bash
journalctl -u lux.service -n 200 --no-pager
```

## 6) Post-Validation Notes
- If send events fail: ensure required properties are populated and consent is true.
- If reporting is empty: confirm the events are written for the target company_id.
