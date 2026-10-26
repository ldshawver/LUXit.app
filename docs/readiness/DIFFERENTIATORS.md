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
| M2 | Relationship intelligence | **Done (branch)** | `services/relationship_intelligence.py`, `models.RelationshipMemory` + `migrations/20261026_relationship_memory.sql` (not applied); `/api/contacts/<id>/briefing`, `/memories/scan`, `/memories`, `/memories/<id>/review`, `/memories/<id>/task`; briefing panel; `tests/test_relationship_intelligence.py` 10 tests |
| M3 | Actionable CRM | **Done (branch)** | `services/crm_workspace.py`, `crm_workspace_bp.py` (`/crm/today`, `/api/crm/dashboard`, `/api/crm/pipeline`, `/api/crm/opportunities[/<id>]`, `/api/crm/tasks[/<id>[/complete|reopen]]`), `templates/crm/today.html`, sidebar "Today"; segment engine relationship fields; scheduler job `crm_task_reminders`; `tests/test_crm_workspace.py` 12 tests |
| M4 | Unified communications | **Done (branch, provider adapters pending)** | `services/unified_comms.py`, `unified_comms_bp.py`, models `BusinessIdentity`/`BusinessIdentityNumber`/`EmailMessage` + `migrations/20261026_comms_identity_email.sql` (not applied); email on the timeline; `docs/contracts/LUXIT_PHONE_CRM_CONTRACT.md`; `tests/test_unified_comms.py` 13 tests |
| M5 | Intelligent automation | **Done (branch)** | `services/crm_automation.py` (extended), `services/crm_events.py`, `services/automation_builder.py`, `automation_builder_bp.py` (`/automations/workflows`, `/api/automations*`), `templates/automations/workflows.html`; event sources in `twilio_sms.py` (missed call, inbound SMS) and `crm_workspace` (opportunity/task); scheduler job `crm_inactivity_scan`; `tests/test_workflow_automation.py` 17 tests |

### M1 detail

- Canonical identity: follows `merged_into_contact_id` (≤10 hops) inside the tenant; includes rows still attached to merged-away contacts (`merged_from`). Multi-phone/email come from `ContactPhoneNumber` / `ContactEmailAddress` (resolver already matches secondary identifiers).
- Sources: ContactActivity, SMS (TwilioMessage via conversation; unlinked conversations by phone), calls (contact or phone match), voicemail (phone match — no contact_id column), ContactTask, Opportunity, DealActivity (via Deal), TouchpointEvent, ContactSourceEvent. Every query filters `company_id`; a failing source is reported under `unavailable` (savepoint) instead of failing the page.
- Paging: `before` cursor (ISO time of the last entry); entries sharing that exact timestamp across a page boundary can be skipped — acceptable for v1.
- Fixed pre-existing defects: log-activity routes set non-existent columns (`subject`, `outcome`, …) that were silently dropped, and `Contact.engagement_score`/`last_activity` (do not exist) made every activity log fail; the activities JSON API raised on `a.subject`. Details now live in `extra_data`; `ContactActivity` exposes read-only `subject/outcome/duration_minutes/is_completed`. Activity lists are now also filtered by company.

### M2 detail

- **Briefing** (deterministic, from the M1 timeline): name/identifiers, consent (do-not-contact, SMS opt-out, email contactability), last inbound/outbound, days since contact, 30-day channel counts, one-line conversation summary, open tasks (overdue flagged), open opportunities, memories, ranked next actions. Every recommendation cites `source_refs`.
- **Next-action rules:** do-not-contact suppresses all outreach; unanswered inbound text → reply (or "reply by phone or email" when opted out of SMS); missed call/voicemail not returned → return call; overdue task; commitment/follow-up due within 2 days; open opportunity with 30+ days of silence → check in.
- **Detection:** outbound texts / logged notes with promise phrases ("I'll send…", "we will call…") → `commitment`; inbound texts/voicemail/call transcripts with asks ("can you…", "call me back…") → `follow_up`; due date from phrases (today, tomorrow, weekday, next week, this week). Stored `pending`, one per source row (`dedupe_key`), rejected items never return.
- **Review:** confirm / correct (keeps original text) / reject / reopen; editors only (viewers read). Make-task converts to a `ContactTask` once.
- **AI:** optional layer, not wired to a provider yet (`ai.status` = `not_configured` / `available_on_request`); OpenAI is out of credit (T-20). When added, AI output must be stored as `origin='ai'`, `pending`, with source refs — same review path.

### M3 detail

- **Pipeline:** `Opportunity` with stages `new_lead → qualified → proposal → negotiation → won/lost` (closing sets status, probability, reason). Every stage change writes a `stage_change` `ContactActivity` (timeline) and a CRM event (`opportunity.stage_changed`, consumed in M5). Board: per-stage count, value, weighted value; owner filter. Owners/assignees must be workspace members.
- **Tasks:** create/update/complete/reopen `ContactTask`; buckets overdue / today (UTC) / upcoming / done; completion logs `task_completed`; `Contact.next_follow_up_at` tracks the earliest open task. Viewers can't change tasks except completing ones assigned to them.
- **Reminders:** `send_due_reminders()` (scheduler job `crm_task_reminders`, every 5 min, pause-preserving like the other jobs) writes one `Notification` to the assignee and clears `reminder_at`, so each fires once. **Deploy note:** this is a new worker job; it only runs where the scheduler runs (prod worker is on stale code — T-18).
- **Today view** (`/crm/today`, mine/everyone): unanswered texts (latest message inbound, 14 d), missed calls not followed by an outbound call/text (7 d), overdue and due-today tasks, commitments/follow-ups due within 2 days, opportunity follow-ups past due, open opportunities with 30+ days of silence; do-not-contact contacts excluded; pipeline summary.
- **Dynamic segmentation:** new fields `lifecycle_stage`, `owner_user_id`, `last_contacted_at`, `last_activity_at`, `next_follow_up_at` (+ relative ops `within_last_days`, `more_than_days_ago`, `within_next_days` evaluated at refresh time), computed `open_opportunity_stage`, `has_open_opportunity`, `has_open_task`, `has_overdue_task` (one query per refresh). Still fail-closed: bad values/ops raise `SegmentConditionError`. Simple text fields now reject date operators at validation instead of at evaluation.

### M4 detail

- **Business identities:** name, sender email/name, signature, default flag, owned phone numbers (a number belongs to one identity; numbers must be the workspace's). `identity_for_number()` routes a business line to its identity (else the default). Admin-only management API.
- **One send path:** `POST /api/contacts/<id>/send` → `send_to_contact()`: refuses do-not-contact, SMS opt-out, email opt-out, missing address, promotional purpose (campaigns own marketing consent), empty/oversized bodies. SMS goes through the existing idempotent conversation path (`send_with_idempotency` → `sendConversationSms`: server-chosen sender, STOP backstop, license gate), only into an existing thread on the identity's line. Email is **off** unless `LUXIT_TENANT_EMAIL_SEND_ENABLED` and the identity has a sender address (current transport is the platform mailbox → needs per-tenant verified sending before enabling).
- **Email history:** `EmailMessage` (idempotent on provider message id per workspace; counterpart matched by primary or secondary address; unknown senders stored unlinked — email never creates contacts). Manual "log an email" API until a mailbox is connected. Timeline + briefing include email.
- **Voice/voicemail/SMS/MMS history:** already in the M1 timeline (MMS media is not surfaced yet).
- **LUXit Phone:** caller-context API + contract doc.
- **Needs approval / credentials:** mailbox adapters (Microsoft Graph / Gmail OAuth app registration per tenant), tenant email sending (verified domains), outbound-first SMS (new-thread creation + 10DLC per tenant), MMS sending through the conversation path.

### M5 detail

- **Engine:** the existing `crm_automation` contract, extended — not replaced. My Order Customer rules and their event conditions are unchanged (covered by a regression test) and stay read-only in the builder; builder rules are `automation_rule` segments with `category='workflow'`.
- **Triggers:** `inbound_sms.received`, `call.missed` (Twilio status callback; key = call SID), `opportunity.created`, `opportunity.stage_changed`, `task.completed`, `contact.inactive` (daily scan; fires once per quiet period per rule), plus existing `tag_added` / first-inbound.
- **Conditions:** tags, segment membership, lifecycle stage, owner, consent (`sms_allowed`/`email_allowed`/`contactable`, do-not-contact never passes), business hours (existing `_is_business_hours`), opportunity stage, from/to stage, no communication for N days, cooldown (no run for this contact in N days), no open tasks.
- **Actions:** create task / follow-up (owner-assigned, `{first_name}`/`{name}` placeholders), internal note, update the event's opportunity, **draft a message for approval** (`message.send` requires `approval: "required"`; approval sends through `unified_comms.send_to_contact`, which re-checks consent, with idempotency key `automation:<execution id>`). Call routing stays in the voice webhook (`receive_calls`), not a workflow action.
- **Reliability:** `CRMAutomationExecution` unique `(company, rule, event_key, action)` = idempotency; each M5 action runs in its own savepoint — a failure marks that execution `failed` with the error and the rest continue; `emit()` never breaks the caller (savepoint + log) and nested events stop at depth 2 (no rule ping-pong). Both new scheduler jobs are pause-preserving.
- **Builder:** structured editor (trigger → conditions → actions), dry-run against a contact (no writes), execution history with status filter, failure summary, approvals queue. Admins manage; editors approve; viewers read. Referenced segments/tags/owners must belong to the workspace.
- **AI-assisted workflows:** not wired (no working AI provider, T-20). Design: AI may only *draft* (`message.send` / memory with `origin='ai'`), never send without approval.

## Verification status (2026-10-26)

- Targeted suites green: timeline 7, relationship intelligence 10, CRM 12, unified comms 13, workflows 17, scheduler 7; adjacent existing suites (contacts, segments, messaging, security positive controls) show only 1c56fbe-baseline failures.
- **Full batched regression of this branch is still owed:** the run was stopped by the harness at batch 1/14 because host memory was low (~690 MB available; another session was active). Batch 0 had no new failures. Rerun with the scratchpad `run_batches.sh` when memory allows.
- Migrations (all additive, none applied): `20261026_relationship_memory.sql`, `20261026_comms_identity_email.sql` (plus the four on the SaaS-launch line).
