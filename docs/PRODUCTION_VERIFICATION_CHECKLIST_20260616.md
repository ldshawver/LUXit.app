# Luxit.app Production Verification Checklist — 2026-06-16

## Current merge status

- Merge recommendation: yes, for audit hardening.
- Production resolved claim: no. The production 500/dashboard issue must not be marked resolved until live deployment evidence is collected.

## Required deployment evidence

Capture all of the following after deploying the approved commit:

1. Deployed commit hash.
2. Migration execution output for:
   - `migrations/20260616_sms_campaign_segment.sql`
   - `migrations/20260616_agent_company_scope.sql`
3. Service restart output for the app process and web server/proxy.
4. Live HTTP checks:
   - `GET /healthz`
   - `GET /__version`
   - `GET /login`
   - `GET /auth/login`
5. Authenticated app checks:
   - `GET /dashboard`
   - marketing dashboard load
   - SMS campaign creation
   - AI report page
   - approval queue
6. Logs reviewed after smoke tests:
   - application logs
   - process manager logs (PM2/systemd/Gunicorn)
   - web server logs (nginx/apache)
   - database/migration output

## Expected outcomes

- `/healthz` returns HTTP 200 and JSON `{"status":"ok"}`.
- `/__version` returns HTTP 200 with `app`, `version`, and `git_sha` keys.
- `/login` and `/auth/login` return HTTP 200 login HTML for unauthenticated users.
- Authenticated `/dashboard`, marketing dashboard, AI report page, and approval queue load without 500 errors.
- SMS campaign creation persists a company-scoped campaign and recipients without placeholder success states.
- Logs contain no new stack traces, migration failures, missing-column errors, tenant lookup errors, or provider-secret leaks.

## Remaining tracked work after merge

1. Complete legacy marketing route-by-route review beyond the already-tested `/api/marketing/*` paths.
2. Finish reviewer assignment by department/type and reviewer notification routing.
3. Finish scheduled-job timezone/idempotency proof for monthly/quarterly AI reports, summaries, analytics, and business reviews.
4. Run credentialed staging tests for WooCommerce and PostHog.
5. Backfill legacy agent artifacts with `NULL company_id` only after an operator confirms the correct company mapping; until then, tenant-scoped UI/API queries intentionally hide those rows.

## Tailscale/GitHub Actions note

For private Tailscale SSH deployment, use the repaired OAuth/tagged `tag:github-actions` runner path, or another approved public SSH endpoint. Direct private-IP SSH still requires the runner to join the tailnet first.
