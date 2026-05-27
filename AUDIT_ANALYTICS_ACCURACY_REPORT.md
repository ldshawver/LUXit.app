# AUDIT — Analytics Accuracy & Live Data
**Date:** 2026-05-02

---

## EXECUTIVE SUMMARY

Most analytics metrics pull from live database records filtered by company_id. The primary accuracy issue is a set of hardcoded fallback values in `routes.py` that substitute mock numbers when a company has zero real data. These must be replaced with `0`. Revenue analytics have no Stripe/MyPayLink feed — they rely on `Deal.value` and `Campaign.revenue_generated` fields only.

---

## METRIC INVENTORY

### CRM / Dashboard Metrics

| Metric | Source Table | Formula | Live? | Company-filtered? | Issue |
|--------|-------------|---------|-------|------------------|-------|
| Open Deals | `deal` | COUNT where stage NOT IN (Closed Won/Lost) | ✅ | ✅ | — |
| Total Leads | `contact` | COUNT | ✅ | ✅ | — |
| Subscribed Contacts | `contact` | COUNT where is_subscribed=True | ✅ | ✅ | — |
| Pending Tasks | `crm_task` | COUNT where status='pending' | ✅ | ✅ | — |
| Upcoming Meetings | `meeting` | COUNT where start_time > now | ✅ | ✅ | — |
| Active Campaigns | `campaign` | COUNT where status='active' | ✅ | ✅ | — |
| Sent Emails | `campaign` | COUNT where status='sent' | ✅ | ✅ | — |

### Analytics Hub Metrics

| Metric | Source | Formula | Live? | Issue |
|--------|--------|---------|-------|-------|
| Website Touchpoints | `touchpoint_event` | COUNT where type='website' | ✅ | **Falls back to `or 142` if 0** |
| Social Touchpoints | `touchpoint_event` | COUNT where type='social' | ✅ | **Falls back to `or 89` if 0** |
| Form Touchpoints | `touchpoint_event` | COUNT where type='form' | ✅ | **Falls back to `or 67` if 0** |
| Email Touchpoints | `touchpoint_event` | COUNT where type='email' | ✅ | **Falls back to `or 234` if 0** |
| Call Touchpoints | `touchpoint_event` | COUNT where type='call' | ✅ | **Falls back to `or 45` if 0** |
| Referral Touchpoints | `touchpoint_event` | COUNT where type='referral' | ✅ | **Falls back to `or 28` if 0** |
| Campaign Open Rate | `email_tracking` | opens/sent | ✅ | Calculated live |
| Campaign CTR | `email_tracking` | clicks/sent | ✅ | Calculated live |
| Lead Score | `lead_score` | Weighted formula | ✅ | Company-filtered |
| Churn Risk | `contact` + activity | Heuristic score | ✅ | Internal model |

### Revenue Metrics

| Metric | Source | Formula | Live? | Issue |
|--------|--------|---------|-------|-------|
| Deal Value (pipeline) | `deal` | SUM of deal.value | ✅ | Company-filtered |
| Campaign Revenue | `campaign.revenue_generated` | Stored on campaign | ✅ | No Stripe feed |
| MRR | **None** | Not calculated | ❌ | Needs Stripe integration |
| ARR | **None** | Not calculated | ❌ | Needs Stripe integration |
| Invoice history | **None** | Not available | ❌ | Needs Stripe integration |

### SMS Analytics (Twilio)

| Metric | Source | Formula | Live? |
|--------|--------|---------|-------|
| Total Messages | `twilio_message` | COUNT | ✅ |
| Inbound vs Outbound | `twilio_message.direction` | COUNT by direction | ✅ |
| Conversations | `twilio_conversation` | COUNT | ✅ |
| Call logs | `twilio_call_log` | COUNT + duration | ✅ |
| Opt-ins/opt-outs | `twilio_conversation` | COUNT by opt fields | ✅ |

### SEO Metrics

| Metric | Source | Live? |
|--------|--------|-------|
| Keywords tracked | `seo_keyword` | ✅ |
| Keyword rankings | `keyword_ranking` | ✅ (manual/API) |
| Backlinks | `seo_backlink` | ✅ |
| Competitors | `seo_competitor` | ✅ |
| SEO audit results | `seo_audit` | ✅ |

### AI Agent Metrics

| Metric | Source | Live? |
|--------|--------|-------|
| Agent reports | `agent_report` | ✅ when OpenAI key valid |
| Agent deliverables | `agent_deliverable` | ✅ |
| Agent performance | `agent_performance` | ✅ |
| Agent executions | `agent_log` | ✅ |

---

## FIXES REQUIRED

### Fix 1 — Remove Hardcoded Fallback Values (P1)

**File:** `routes.py` lines ~370-375

**Current:**
```python
'website': TouchpointEvent.query.filter_by(company_id=company_id, touchpoint_type='website').count() or 142,
'social':  TouchpointEvent.query.filter_by(company_id=company_id, touchpoint_type='social').count() or 89,
'forms':   TouchpointEvent.query.filter_by(company_id=company_id, touchpoint_type='form').count() or 67,
'email':   TouchpointEvent.query.filter_by(company_id=company_id, touchpoint_type='email').count() or 234,
'calls':   TouchpointEvent.query.filter_by(company_id=company_id, touchpoint_type='call').count() or 45,
'referral':TouchpointEvent.query.filter_by(company_id=company_id, touchpoint_type='referral').count() or 28
```

**Required:**
```python
'website': TouchpointEvent.query.filter_by(company_id=company_id, touchpoint_type='website').count(),
'social':  TouchpointEvent.query.filter_by(company_id=company_id, touchpoint_type='social').count(),
...
```

### Fix 2 — Add Revenue Analytics from Stripe (P1)

Add Stripe API calls for:
- `MRR` = sum of active subscription amounts
- `ARR` = MRR × 12
- Invoice history with status
- Failed payment count

### Fix 3 — Add GA4 Data Pull (P2)

Replace static Google Analytics config storage with actual GA4 Reporting API calls for sessions, conversions, bounce rate.

---

## REFRESH FREQUENCIES

| Metric Category | Refresh | Cache |
|----------------|---------|-------|
| CRM (deals, contacts, tasks) | Real-time | None |
| Campaign metrics | Real-time | None |
| AI agent reports | Scheduled (daily/weekly) | DB stored |
| SEO metrics | Manual / API | DB stored |
| Analytics hub | Real-time | None |
| SMS analytics | Real-time | None |
