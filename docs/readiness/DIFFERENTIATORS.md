# LUXit.app — Differentiators Tracker

Resume point for the differentiation milestones (M1–M5).

- **Branch:** `feat/differentiators-20261026`, worktree `/root/luxit-differentiators`, based on `4727798` (SaaS-launch line: prod `1c56fbe` + 9 local commits). Local commits only; nothing pushed or deployed.
- **Architecture map:** reuse, never duplicate (2026-10-23 map).

## Decisions (made 2026-10-26, revisit only with a reason)

| Topic | Decision | Why |
|---|---|---|
| Timeline sink | `ContactActivity` for new manual/system activity; timeline is a **read model** over existing tables, not a copy | no backfill, no drift, every entry cites its source row |
| Pipeline | `Opportunity` is canonical (company_id NOT NULL, owner, follow-up). Legacy `Deal`/`DealActivity` stay readable and appear in the timeline | `Deal` has nullable company_id and no owner |
| Tasks | `ContactTask` is canonical; `CRMTask` not extended | company-scoped, contact-linked |
| Automation | extend `services/crm_automation.py` (validated contract, idempotent `CRMAutomationExecution.event_key`); do not revive `automation_service` / `workflow_builder_service` / `AgentAutomation` | only engine with idempotency |
| `ActivityLog` | not part of a contact timeline | per-user audit log, no contact reference |
| AI | every AI-derived item is stored as reviewable (pending/confirmed/rejected) with source refs; deterministic facts first, AI optional | OpenAI account out of credit (T-20); output must be correctable |

## Milestones

| # | Milestone | Status | Evidence |
|---|---|---|---|
| M1 | Unified contact identity + relationship timeline | **Done (branch)** | `services/contact_timeline.py`, `GET /api/contacts/<id>/timeline`, timeline panel on `/contacts/<id>/activities`; `tests/test_contact_timeline.py` 7 tests |
| M2 | Relationship intelligence | Not started | |
| M3 | Actionable CRM | Not started | |
| M4 | Unified communications | Not started | |
| M5 | Intelligent automation | Not started | |

### M1 detail

- Canonical identity: follows `merged_into_contact_id` (≤10 hops) inside the tenant; includes rows still attached to merged-away contacts (`merged_from`). Multi-phone/email come from `ContactPhoneNumber` / `ContactEmailAddress` (resolver already matches secondary identifiers).
- Sources: ContactActivity, SMS (TwilioMessage via conversation; unlinked conversations by phone), calls (contact or phone match), voicemail (phone match — no contact_id column), ContactTask, Opportunity, DealActivity (via Deal), TouchpointEvent, ContactSourceEvent. Every query filters `company_id`; a failing source is reported under `unavailable` (savepoint) instead of failing the page.
- Paging: `before` cursor (ISO time of the last entry); entries sharing that exact timestamp across a page boundary can be skipped — acceptable for v1.
- Fixed pre-existing defects: log-activity routes set non-existent columns (`subject`, `outcome`, …) that were silently dropped, and `Contact.engagement_score`/`last_activity` (do not exist) made every activity log fail; the activities JSON API raised on `a.subject`. Details now live in `extra_data`; `ContactActivity` exposes read-only `subject/outcome/duration_minutes/is_completed`. Activity lists are now also filtered by company.
