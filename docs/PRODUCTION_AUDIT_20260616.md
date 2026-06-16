# Luxit.app Production Audit Follow-up — 2026-06-16

## Production 500 status

This follow-up keeps the previous fixes for the dashboard boot imports, smoke routes, passwordless login feedback, dashboard company scoping, and `SMSCampaign.segment` schema mismatch. In this workspace, the app factory, login, dashboard, and health paths now compile and pass smoke/route tests. Production confirmation still requires deploying this branch, applying the included migrations, restarting the service, and checking `GET /healthz`, `GET /auth/login`, `GET /login`, and an authenticated `GET /dashboard` on the live host.

## Migrations to apply/verify

1. `migrations/20260616_sms_campaign_segment.sql` — adds `sms_campaign.segment` for segment-targeted SMS campaign APIs.
2. `migrations/20260616_agent_company_scope.sql` — adds `agent_report.company_id`, `agent_log.company_id`, `agent_deliverable.company_id`, `agent_deliverable.priority`, and `agent_deliverable.requested_by_id` plus indexes for agent artifact tenant isolation.

Startup self-heal DDL also includes the agent artifact columns for cold-start safety, but production should still run the migration SQL explicitly during deploy.

## Migration safety review

- `20260616_sms_campaign_segment.sql` uses only `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`; it is safe to re-run and does not rewrite or delete existing SMS campaign data.
- `20260616_agent_company_scope.sql` uses only `ADD COLUMN IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`; it is safe to re-run and does not backfill, overwrite, or delete existing agent data. Legacy agent artifacts with NULL `company_id` are intentionally hidden from tenant-scoped UI/API reads until an operator-approved backfill maps them to the correct company.

## Marketing audit findings

- The `/api/marketing/*` SMS, keyword-rule, auto-reply, social, and audit paths use the logged-in user's tenant via `tenant_id()` and filter core records by `company_id`.
- SMS campaign CRUD, preview, send, STOP/START/HELP handling, keyword rules, audit reads, and tenant isolation are covered by `tests/test_marketing_hub_regression.py`.
- SMS sending remains idempotency-tested by `tests/test_sms_campaign_double_send.py`.
- Missing Twilio credentials return structured failures and do not create fake success states.
- Some legacy marketing pages outside `/api/marketing/*` still need deeper line-by-line review for all reads because the repo contains older page routes with mixed tenant-resolution patterns.

## AI-agent audit findings

- Durable DB-backed models exist for `AgentTask`, `AgentReport`, `AgentDeliverable`, `AgentReview`, logs, memory, conversations, and performance.
- This follow-up repaired AI report tenant isolation by adding `AgentReport.company_id` and scoping list/detail routes to the active company.
- This follow-up also added `AgentLog.company_id` and scoped AI activity/performance APIs plus legacy agent report/detail pages where company context is available.
- This follow-up repaired AI deliverable creation by adding the model fields that the route already used (`priority`, `requested_by_id`) and retaining `company_id` scoping.
- AI report generation still falls back to stored placeholder/error text when OpenAI is not configured. That is durable, not a fake external-send success, but it should be labeled in UI as provider-not-configured output.
- Remaining risk: background agent writers still need a full pass to ensure every newly-created `AgentLog` receives `company_id`; legacy rows with NULL company_id are intentionally hidden from tenant-scoped UI/API queries.

## Reviewer/approval workflow and notification audit findings

- Approval queue models and audit-log models exist and approval actions write audit rows through `ApprovalService`.
- This follow-up fixed route-level approval queue tenant leakage where routes used `Company.query.first()` by switching list/detail/action/stats/toggle paths to the active company.
- Approval item detail/action routes now 404 cross-tenant IDs before calling mutating service methods.
- In-app notifications are user-scoped for mark-read/delete operations; a regression test confirms one user cannot mark another user's notification as read.
- Reviewer assignment by department/type is not fully implemented as a durable assignment model in the audited code; treat that requirement as incomplete until reviewer routing rules are added.

## Scheduled jobs / automation audit findings

- Agent scheduling exists in `agent_scheduler.py`, and SMS campaign send idempotency is covered by tests.
- Twilio after-hours behavior has timezone-focused tests in `tests/test_sms_campaign_after_hours.py`.
- Company timezone handling is present in Twilio/business-hours models, but agent scheduled jobs still need a deeper pass to ensure every monthly/quarterly task uses company-local time and idempotency keys.

## Provider-missing behavior audit findings

- Twilio/SMS: missing credentials return structured errors and tests assert missing-config behavior.
- MS Graph/Outlook: this follow-up updates the test contract to assert documented `missing_config` when `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, or `MS_TENANT_ID` are absent.
- Stripe: health/ready endpoints expose boolean configuration status without logging secret values.
- OpenAI: health reports enabled/disabled by provider config; AI report generation stores a provider-not-configured message when no key is available.
- WooCommerce/PostHog: missing credentials now have direct no-crash regression tests; live credentialed behavior still requires provider-specific staging tests.

## Deployment note update

Do not describe `100.85.15.43` as categorically unreachable from GitHub Actions anymore. The deployment path has been repaired for MyOrder.fun using Tailscale OAuth/tagged access. For Luxit.app, use the same OAuth/tagged `tag:github-actions` pattern or another approved public SSH endpoint; direct SSH to a private Tailscale IP still requires the runner to join the tailnet first.

## Remaining incomplete items

- Live production verification after deploy, including log review from app/process/web-server/database layers. Use `docs/PRODUCTION_VERIFICATION_CHECKLIST_20260616.md` for required evidence.
- Full route-by-route audit of older marketing pages beyond the covered `/api/marketing/*` regression surface.
- Complete reviewer assignment by department/type.
- Full company-timezone/idempotency proof for every agent and scheduled publishing job.
- Live credentialed staging tests for WooCommerce and PostHog provider behavior.
