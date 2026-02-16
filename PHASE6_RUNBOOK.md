# Phase 6 Runbook (v3.8.6) — Creative Fatigue Detection

> Purpose: Validate creative fatigue signals and CTR drop detection on the VPS.
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

## 3) Creative Event Ingestion (Sample)
> Replace `company_id` with a valid company in your database.
```bash
# Impression events (baseline)
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "ad_impression",
    "consent": true,
    "session_id": "sess_phase6_impressions",
    "properties": {
      "creative_id": "creative_123",
      "angle": "productivity"
    }
  }'

# Click event (lower CTR)
curl -sSf -X POST http://127.0.0.1:5000/e \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "event_name": "ad_click",
    "consent": true,
    "session_id": "sess_phase6_clicks",
    "properties": {
      "creative_id": "creative_123",
      "angle": "productivity"
    }
  }'
```

## 4) Report Validation
```bash
curl -sSf "http://127.0.0.1:5000/analytics/report?company_id=1" >/dev/null
```

## 5) Logs (Debugging)
```bash
journalctl -u lux.service -n 200 --no-pager
```

## 6) Post-Validation Notes
- If creative IDs do not appear in reports, confirm event properties include `creative_id`.
- If fatigue detection is missing, ensure CTR drop logic is enabled for Phase 6.
