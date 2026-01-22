# Phase 2 Runbook (v3.8.2) — Commerce + Lead Events

> Purpose: Validate commerce + lead event ingestion and order capture on the VPS.

## 1) Pre-flight (Service + Env)
```bash
cd /opt/luxit
source /opt/luxit/venv/bin/activate
python -m py_compile app.py auth.py wsgi.py
systemctl status lux.service --no-pager -l
```

## 2) Health + Auth Smoke Check
```bash
curl -sSf http://127.0.0.1:5000/ >/dev/null
curl -sSf http://127.0.0.1:5000/auth/login >/dev/null
```

## 3) Lead + Commerce Event Ingestion
> Replace `company_id` with a valid company in your database.
```bash
# lead_submit
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "lead_submit",
    "consent": true,
    "session_id": "sess_phase2_lead",
    "page_url": "https://luxit.app/",
    "properties": {
      "lead_type": "demo_request",
      "source": "homepage_form"
    }
  }'

# add_to_cart
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "add_to_cart",
    "consent": true,
    "session_id": "sess_phase2_cart",
    "page_url": "https://luxit.app/pricing",
    "properties": {
      "product_id": "sku_123",
      "value": 129.00,
      "currency": "USD"
    }
  }'

# begin_checkout
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "begin_checkout",
    "consent": true,
    "session_id": "sess_phase2_checkout",
    "page_url": "https://luxit.app/checkout",
    "properties": {
      "cart_value": 129.00,
      "currency": "USD"
    }
  }'

# purchase
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "purchase",
    "consent": true,
    "session_id": "sess_phase2_purchase",
    "page_url": "https://luxit.app/thank-you",
    "properties": {
      "order_id": "order_123",
      "value": 129.00,
      "currency": "USD"
    }
  }'
```

## 4) Export + Report Validation
```bash
curl -sSf "http://127.0.0.1:5000/analytics/report?company_id=1" >/dev/null
curl -sSf "http://127.0.0.1:5000/analytics/report/export/csv?company_id=1" -o /tmp/analytics_phase2.csv
```

## 5) Logs (Debugging)
```bash
journalctl -u lux.service -n 200 --no-pager
```

## 6) Post-Validation Notes
- If purchase events fail: verify required properties (`order_id`, `value`, `currency`).
- If reports fail: ensure the company has data and event filters are correct.
