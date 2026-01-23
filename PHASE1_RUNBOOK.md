# Phase 1 Runbook (v3.8.1) — First-Party Analytics Foundation

> Purpose: Validate analytics ingestion, consent enforcement, and reporting flows on the VPS.

## 1) Pre-flight (Service + Env)
```bash
cd /opt/luxit
source /opt/luxit/venv/bin/activate
  12555wpython -m py_compile app.py auth.py wsgi.py
systemctl status lux.service --no-pager -l
```

## 2) Health + Auth Smoke Check
```bash
curl -sSf http://127.0.0.1:5000/ >/dev/null
curl -sSf http://127.0.0.1:5000/auth/login >/dev/null
```

## 3) First-Party Event Ingestion
> Replace `company_id` with a valid company in your database.
```bash
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "page_view",
    "consent": true,
    "session_id": "sess_phase1_smoke",
    "page_url": "https://luxit.app/",
    "referrer": "https://luxit.app/",
    "utm_source": "newsletter",
    "utm_medium": "email",
    "utm_campaign": "phase1",
    "device_type": "mobile",
    "viewport_width": 390,
    "orientation": "portrait"
  }'
```

## 4) Consent/GPC Suppression (Expected Drop)
> This request should be rejected or recorded as suppressed per consent rules.
```bash
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -H "Sec-GPC: 1" \
  -d '{
    "company_id": 1,
    "event_name": "page_view",
    "consent": false,
    "session_id": "sess_phase1_gpc",
    "page_url": "https://luxit.app/",
    "device_type": "mobile"
  }'
```

## 5) Report + Export Endpoints
```bash
curl -sSf "http://127.0.0.1:5000/analytics/report?company_id=1" >/dev/null
curl -sSf "http://127.0.0.1:5000/analytics/report/export/csv?company_id=1" -o /tmp/analytics_phase1.csv
curl -sSf "http://127.0.0.1:5000/analytics/report/export/excel?company_id=1" -o /tmp/analytics_phase1.xlsx
curl -sSf "http://127.0.0.1:5000/analytics/report/export/pdf?company_id=1" -o /tmp/analytics_phase1.pdf
```

## 6) Print View
```bash
curl -sSf "http://127.0.0.1:5000/analytics/report/print?company_id=1" >/dev/null
```

## 7) Logs (Debugging)
```bash
journalctl -u lux.service -n 200 --no-pager
```

## 8) Post-Validation Notes
- If `/e` fails: verify database connectivity and `company_id`.
- If exports fail: confirm export libraries are installed and file permissions are writable.
- If consent/GPC suppression is not honored: validate request headers and consent flags.
