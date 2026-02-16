# Phase 3 Runbook (v3.8.3) — Attribution v1

> Purpose: Validate first-touch/last-touch attribution with a 30-day lookback and 72-hour last-touch window.

## 1) Pre-flight (Service + Env)
```bash
cd /opt/luxit
source /opt/luxit/venv/bin/activate
python -m py_compile app.py auth.py wsgi.py
systemctl status lux.service --no-pager -l
```

## 2) Seed Sessions + Purchase Flow
> Replace `company_id` with a valid company in your database.
```bash
# Session 1 (first touch)
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "page_view",
    "consent": true,
    "session_id": "sess_phase3_first",
    "page_url": "https://luxit.app/",
    "utm_source": "google",
    "utm_medium": "cpc",
    "utm_campaign": "brand"
  }'

# Session 2 (last touch)
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "page_view",
    "consent": true,
    "session_id": "sess_phase3_last",
    "page_url": "https://luxit.app/pricing",
    "utm_source": "newsletter",
    "utm_medium": "email",
    "utm_campaign": "launch"
  }'

# Purchase event
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "purchase",
    "consent": true,
    "session_id": "sess_phase3_last",
    "page_url": "https://luxit.app/thank-you",
    "properties": {
      "order_id": "order_phase3",
      "value": 399.00,
      "currency": "USD"
    }
  }'
```

## 3) Attribution Check (Database)
```bash
psql "$DATABASE_URL" -c "\
  SELECT order_id, first_touch_source, last_touch_source, attributed_at \
  FROM orders \
  WHERE order_id = 'order_phase3';\
"
```

## 4) Logs (Debugging)
```bash
journalctl -u lux.service -n 200 --no-pager
```

## 5) Post-Validation Notes
- If attribution fields are null, confirm the lookback window and session timestamps.
- If order rows are missing, confirm purchase events are being persisted.
