# Phase 7 Runbook (v3.8.7) — Alerts + Insights

> Purpose: Validate alert thresholds and insight generation on the VPS.
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

## 3) Trigger Conditions (Sample Events)
> Replace `company_id` with a valid company in your database.
```bash
# CPA spike signal
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "purchase",
    "consent": true,
    "session_id": "sess_phase7_purchase",
    "properties": {
      "order_id": "order_phase7",
      "value": 49.00,
      "currency": "USD",
      "utm_source": "paid_search",
      "utm_medium": "cpc"
    }
  }'
```

## 4) Alerts Endpoint (If Available)
```bash
curl -sSf "http://127.0.0.1:5000/api/alerts?company_id=1" >/dev/null
```

## 5) Logs (Debugging)
```bash
journalctl -u lux.service -n 200 --no-pager
```

## 6) Post-Validation Notes
- If alerts are missing, verify alert thresholds and scheduled checks are enabled.
- If alerts are noisy, review thresholds for CPA/CTR/abandonment spikes.
