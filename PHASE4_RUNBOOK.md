# Phase 4 Runbook (v3.8.4) — Rollups + Dashboards + Exports

> Purpose: Validate rollup generation, dashboard filtering, exports, and print view on the VPS.

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

## 3) Dashboard Filters (Time Range + Compare)
> Replace `company_id` with a valid company in your database.
```bash
# Last 30 days, compare to previous period
curl -sSf "http://127.0.0.1:5000/analytics/report?company_id=1&range=last_month&compare=previous_period" >/dev/null

# This quarter, compare to same period last year
curl -sSf "http://127.0.0.1:5000/analytics/report?company_id=1&range=this_quarter&compare=same_period_last_year" >/dev/null
```

## 4) Exports (CSV/Excel/PDF)
```bash
curl -sSf "http://127.0.0.1:5000/analytics/report/export/csv?company_id=1" -o /tmp/analytics_phase4.csv
curl -sSf "http://127.0.0.1:5000/analytics/report/export/excel?company_id=1" -o /tmp/analytics_phase4.xlsx
curl -sSf "http://127.0.0.1:5000/analytics/report/export/pdf?company_id=1" -o /tmp/analytics_phase4.pdf
```

## 5) Print View
```bash
curl -sSf "http://127.0.0.1:5000/analytics/report/print?company_id=1" >/dev/null
```

## 6) Logs (Debugging)
```bash
journalctl -u lux.service -n 200 --no-pager
```

## 7) Post-Validation Notes
- If exports fail: confirm CSV/Excel/PDF dependencies exist on the VPS.
- If compare ranges fail: ensure date-range parameters are valid and data exists.
