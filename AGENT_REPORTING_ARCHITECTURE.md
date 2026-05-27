# AGENT REPORTING ARCHITECTURE
**Date:** 2026-05-02

---

## OVERVIEW

LUXit has 11 AI agents implemented in `agent_scheduler.py` and `ai_agent.py`. Each agent runs on a schedule using APScheduler, generates reports stored in the `agent_report` table, and creates deliverables in the `agent_deliverable` table. All deliverables flow through the Approval Queue before going live.

**Key dependency:** All agents require a valid OpenAI API key. Current dev environment has an invalid key (401 errors in logs). Set valid key on VPS.

---

## AGENT INVENTORY

### 1. Brand Strategy Agent
- **Schedule:** Quarterly planning + Monthly research
- **Focus:** Brand positioning, audience segments, market perception, competitive positioning, messaging
- **Internal data:** Company info, competitor snapshots, campaign performance
- **Report sections:** Executive summary, brand positioning analysis, audience segments, competitor landscape, messaging recommendations
- **Deliverables:** Brand guidelines draft, positioning statements, messaging framework, campaign briefs
- **Table:** `agent_report` where agent_type='brand_strategy'

### 2. Content & SEO Agent
- **Schedule:** Weekly blog post + Monthly calendar
- **Focus:** Website/blog performance, keyword opportunities, content gaps, SEO recommendations
- **Internal data:** `blog_post`, `seo_keyword`, `keyword_ranking`, `seo_competitor`
- **Report sections:** Keyword performance, content gap analysis, SEO recommendations, monthly content plan
- **Deliverables:** Blog post drafts, content calendar, SEO audit reports, meta descriptions

### 3. Analytics Agent
- **Schedule:** Daily recommendations + Weekly summary + Monthly report
- **Focus:** KPI performance, campaign ROI, conversion trends, revenue attribution, forecasting
- **Internal data:** `campaign`, `contact`, `deal`, `email_tracking`, `analytics_data`
- **Report sections:** KPI dashboard, campaign performance, conversion analysis, forecasts
- **Deliverables:** Analytics reports, dashboard summaries, ROI analysis

### 4. Creative Agent
- **Schedule:** Weekly assets
- **Focus:** Visual/campaign collateral, ad creatives, brand assets, design recommendations
- **Internal data:** `agent_deliverable` history, campaign performance
- **Deliverables:** Creative briefs, ad copy, landing page copy, visual direction guides

### 5. Advertising Agent
- **Schedule:** Weekly strategy review
- **Focus:** Paid campaign strategy, ad platform performance, audience targeting, budget allocation
- **Internal data:** Campaign data, lead scores, conversion data
- **Deliverables:** Ad strategy documents, audience targeting specs, budget allocation recommendations

### 6. Social Media Agent
- **Schedule:** Daily posts
- **Focus:** Social performance, post/campaign suggestions, platform trends, engagement strategy
- **Internal data:** `social_post`, `social_media_account`, engagement metrics
- **Deliverables:** Social post drafts, social calendar, engagement strategy docs

### 7. Email CRM Agent
- **Schedule:** Weekly campaign + Daily subscriber sync
- **Focus:** List growth, subscriber health, segmentation, campaign performance, automation
- **Internal data:** `contact`, `campaign`, `campaign_recipient`, `email_tracking`, `segment`
- **Deliverables:** Email campaign drafts, segmentation plans, automation recommendations

### 8. Sales Enablement Agent
- **Schedule:** Weekly lead scoring
- **Focus:** Lead quality, pipeline performance, sales collateral, conversion opportunities
- **Internal data:** `deal`, `contact`, `lead_score`, `crm_task`, `meeting`
- **Deliverables:** Lead scoring reports, sales scripts, follow-up sequences, deal analysis

### 9. Retention Agent
- **Schedule:** Monthly churn analysis
- **Focus:** Churn risks, loyalty opportunities, repeat engagement, customer lifecycle
- **Internal data:** `contact`, `email_tracking`, `touchpoint_event`, activity data
- **Deliverables:** Churn risk report, win-back campaigns, loyalty program recommendations

### 10. Operations Agent
- **Schedule:** Daily health check
- **Focus:** Workflow gaps, integration health, automation opportunities, bottlenecks
- **Internal data:** `automation`, `automation_execution`, `integration_audit_log`, `feature_toggle`
- **Deliverables:** Operations health reports, workflow gap analysis, integration status

### 11. App Intelligence Agent
- **Schedule:** Hourly health check + Daily usage + Weekly improvements
- **Focus:** App health, broken features, UX friction, system performance, improvement recommendations
- **Internal data:** Error logs, route usage, system health
- **Deliverables:** App health reports, improvement recommendations, bug reports

---

## REPORT STRUCTURE (ALL AGENTS)

Every agent report stored in `agent_report` should contain:

```json
{
  "executive_summary": "...",
  "internal_performance_review": "...",
  "external_market_review": "...",
  "competitor_analysis": "...",
  "key_findings": ["..."],
  "risks_opportunities": {"risks": [...], "opportunities": [...]},
  "recommended_actions": ["..."],
  "suggested_campaigns": ["..."],
  "expected_outcomes": {"kpis": {...}},
  "deliverables_to_create": ["..."],
  "approval_items_needed": ["..."],
  "comparison_to_prior": "...",
  "lessons_learned": "..."
}
```

---

## DELIVERABLE → APPROVAL WORKFLOW

```
Agent generates deliverable
  → AgentDeliverable created (status='draft')
  → ApprovalQueue entry created (status='pending')
  → Notification sent to company admins
  → Admin reviews at /approval-queue
    → Approve: content.status = 'approved', triggers publishing/scheduling
    → Reject: feedback stored, agent can learn from it
    → Edit: content updated, re-submitted
    → Cancel: content.status = 'canceled'
  → Approved content connects to:
    - Email campaigns (via Campaign model)
    - SMS campaigns (via SMSCampaign model)
    - Social posts (via SocialPost model)
    - Blog posts (via BlogPost model)
```

---

## REPORT SCHEDULES

| Agent | Monthly | Quarterly | Yearly |
|-------|---------|-----------|--------|
| Brand Strategy | ✅ Research | ✅ Planning | ⚠️ Not scheduled |
| Content & SEO | ✅ Calendar | — | ⚠️ Not scheduled |
| Analytics | ✅ Report | — | ⚠️ Not scheduled |
| Creative | ✅ via weekly | — | ⚠️ Not scheduled |
| Advertising | ✅ via weekly | — | ⚠️ Not scheduled |
| Social Media | ✅ via daily | — | ⚠️ Not scheduled |
| Email CRM | ✅ via weekly | — | ⚠️ Not scheduled |
| Sales Enablement | ✅ via weekly | — | ⚠️ Not scheduled |
| Retention | ✅ Churn | ⚠️ Not scheduled | ⚠️ Not scheduled |
| Operations | ✅ via daily | — | ⚠️ Not scheduled |
| App Intelligence | ✅ via weekly | — | ⚠️ Not scheduled |

**Gap:** No agent has a yearly report scheduled. Add yearly cron triggers in `agent_scheduler.py`.

---

## FIXES REQUIRED

| Priority | Fix |
|----------|-----|
| P0 | Set valid OpenAI API key on VPS |
| P1 | Add yearly report schedule to all agents |
| P1 | Ensure reports include: competitor analysis, external market data, prior report comparison |
| P2 | Add `lessons_learned` field to AgentReport model |
| P2 | Add `comparison_to_prior` auto-population (fetch last report of same type) |
| P3 | Add quarterly schedules for remaining agents |
