# LUXit.app — Launch Readiness Checklist

Living document. Update the "As of" line and the tables whenever a gate moves.
Another session must be able to resume from this file alone.

- **As of:** 2026-10-19 18:15 UTC (Stage A discovery + Stage B security stabilization + P0 containment)
- **Branch:** `saas/gtm-readiness-20261019` (worktree `/root/luxit-gtm-readiness`), based on prod `645dd65`
- **Verdict:** **NO-GO.** Details in the go/no-go section.

---

## 0. Environment and dependency map (verified 2026-10-19)

| Thing | Value |
|---|---|
| Host | single VPS `luxit`, Debian 13, 99 GB disk (52% used). **Rebooted 2026-10-17 04:06 UTC.** |
| Reverse proxy | nginx → `luxit.app`, `www`, `app.luxit.app` → 127.0.0.1:8001 |
| **PROD web** | `lux-email-bot.service`, gunicorn 4×8 gthread :8001, cwd `/root/luxit-prod-645dd65-20261006` (detached `645dd65`), env `/etc/luxit/prod.env`, venv `/root/lux-email-bot/.venv`. Effective config = base unit + 4 drop-ins (last wins: `zz-release-645dd65-20261006.conf`). |
| **PROD worker** | `luxit-tuya-notification-worker.service`, `python -m scripts.run_durable_scheduler`, cwd `/root/luxit-main-canonicalize` (on-disk `eea5b84`, branch `fix/pwa-incoming-ring-audio`), env `/root/lux-email-bot/.env`. Runs Tuya relay (1 s) + `weekly_contact_dedupe` (Sun 03:00 UTC). |
| **Ghost web** | `luxit.service`, gunicorn :8000, cwd `/root/lux-email-bot` (`ea388ff`), prod `.env` + **plaintext ENCRYPTION_MASTER_KEY in a 0644 unit file**. Not proxied by nginx (no traffic since 2026-07-18), but runs a **second Agent Scheduler against the prod DB** → every agent job runs twice. |
| Staging | `lux-email-bot-staging.service` :8004, `/srv/lux-email-bot-staging` @ `c2eda12` (separate repo; contains 63d55ea + phone P0 + phone-native + LUX Connect native; **does NOT contain the 645dd65 email-suppression fix**). DB `luxstaging` on PG :5434. Twilio mode disabled. No Stripe keys. |
| Dev | `lux-email-bot-dev.service` :8002, `/srv/lux-email-bot-dev` @ `f71f68d`. DB `luxdb_dev` on PG :5433. No Stripe keys. |
| Databases | PG17 clusters: main :5432 (`luxdb`, 48 MB, 204 tables, 41 ledgered migrations), luxdev :5433, luxstaging :5434 |
| GitHub | `git@github.com:ldshawver/LUXit.app.git`; `origin/main` = `83a573b`. **Prod `645dd65` and its 6 parents are NOT on GitHub** (DR gap re-opened). Deploy workflow is `workflow_dispatch` only; do not use it (it `reset --hard`s the canonical dir). |
| Prod tenants | 2 real (id 1 "Lucifer Cruz", id 2 "Adiken Inc"), 4 retired smoke tenants; 7 users (only user 1 is platform admin) |
| External services (prod env keys) | Twilio (SMS/Voice, Messaging Service), Microsoft Graph + SMTP (email), Stripe **live** keys, OpenAI (**out of credits**), Google OAuth/Contacts, PostHog, Tuya, Documenso, WooCommerce, X/Instagram/TikTok/Facebook, Bitly, Unsplash, ad networks |
| Native client | LUX Connect (Expo) in `/root/luxit-connect-native*` → bearer API `/api/mobile/v1`; separate client, not part of this release |

## 1. Go / no-go

**NO-GO for general availability.** Each Phase 10 criterion:

| Criterion | Status | Evidence / reason |
|---|---|---|
| All P0 defects closed | **FAIL** | 7 security P0s fixed in branch but **not deployed**; 6 operational P0s need authorization (§3) |
| Registration and onboarding end-to-end | **FAIL** | No self-serve signup. `/auth/register` = first-admin bootstrap only, closed once an admin exists. Pricing CTA = "Book a demo". No email verification. |
| Multi-tenant isolation passes security tests | **FAIL (prod) / PASS (branch, scoped)** | Branch: 25 isolation/authz regression tests pass. Prod still has the IDORs. 7 feature models have no `company_id` at all (§3 P1-7). |
| Core CRM works reliably | **PARTIAL** | Contacts/segments/audiences work (prod usage). Deals pipeline 500s everywhere (fixed in branch). User edit 500s everywhere (fixed in branch). |
| Advertised communications work | **FAIL** | Inbound calling: 1 of 26 inbound calls answered in 14 days (11 no-answer, 7 voicemail, 7 stuck "ringing"). Email campaigns: no delivery path exists. SMS: works. |
| Consent and suppression enforced | **PARTIAL** | SMS STOP + promo opt-in enforced. Email: no working unsubscribe in prod (placeholder link) — implemented in branch. |
| Subscription/payment validated | **FAIL** | Never exercised: 0 successful Stripe events ever, 0 Stripe customers. No sandbox keys on staging/dev. |
| Critical background jobs reliable | **FAIL** | Dedupe pause lost on reboot (ran 2026-10-18, merged 0); worker runs stale code; duplicate agent scheduler; SMS campaign sends run in web-worker daemon threads. |
| Backups restore-tested | **FAIL** | No scheduled backups. Newest full dump 2026-09-08, same disk, no off-site. Restore drill of that dump PASSED (4 s, 203 tables). Backup timer written, not installed. |
| Monitoring and incident procedures | **FAIL** | `/health/deep` 500s in prod (missing helper; fixed in branch). No alerting found. |
| No critical/high security findings | **FAIL** | See §3 and §6 |
| Customer-facing docs accurate | **FAIL** | Site claims rate limiting, CSRF on all endpoints, HubSpot/Salesforce import, Gmail/Outlook auto-logging, AI lead scoring, a testimonial of unknown origin; pricing sells AI that has no credits. |
| Staging acceptance | **NOT STARTED** | Branch not deployed anywhere |
| Rollback plan verified | **PARTIAL** | Per-release detached dirs + drop-ins exist (`/root/luxit-prod-63d55ea-20260928` is the previous prod) |
| Production release authorized | **NO** | — |

## 2. Feature inventory (evidence-based)

Legend: ✅ verified working · 🟡 works with defects · 🟠 partial · ⬜ UI/placeholder only · 🔒 backend-only · ❌ missing · 🗑 redundant

### Accounts, auth, tenancy
| Feature | Status | Evidence |
|---|---|---|
| Login / logout | ✅ | live |
| Self-serve registration | ❌ | bootstrap-only route |
| Email verification | ❌ | none |
| Password reset | 🟡→fixed in branch | prod renders the reset link on screen (account takeover) |
| Forgot username | 🟡→fixed in branch | prod displays username for any email |
| MFA | ❌ | none |
| Rate limiting | ❌→added in branch | none in prod |
| Session revocation | ✅ | `session_revoked_at` honored per request; branch also revokes on reset |
| Team invitations | ❌ | admins create users with a password directly |
| Roles / RBAC | 🟡 | `UserCompanyAccess` roles; user edit 500s in prod; legacy routes bypass all checks (fixed in branch) |
| Platform admin console (`/saas`) | 🟡→fixed in branch | reachable by any signed-in user in prod |
| Account deletion / suspension | 🟠 | archive-based user removal; no tenant deletion flow |

### CRM
| Feature | Status | Evidence |
|---|---|---|
| Contacts CRUD, canonical identity, dedupe | ✅/🟡 | 2,265 prod contacts; dedupe job safety issue (§3 P0-11) |
| Segments + audiences | ✅ | used for SMS, canonical resolver |
| Contact activities | 🟡→fixed | cross-tenant IDOR |
| Deals pipeline | 🟡→fixed in branch | `DealActivity` model never existed → every deal page/API 500; 0 deals in prod |
| Unified CRM page `/crm-unified` | 🟡→fixed | undefined name `segment` → always 500 |
| Lead scoring | 🟠 | `LeadScore` table exists; AI scoring claim unverified |
| Google Contacts sync | ✅ (per prior sessions) | — |
| CSV import | not verified this session | — |

### Communications
| Feature | Status | Evidence |
|---|---|---|
| Two-way SMS + MMS | ✅ | 5,731 messages in prod, signed status callbacks |
| SMS campaigns (UI path) | 🟡 | sends via `SMSService` in a web-worker daemon thread; 2 June/July "scheduled" campaigns never sent; campaign id 1 has `company_id` NULL |
| SMS campaigns (API `/api/marketing/sms-campaigns/<id>/send`) | 🟡 | imports nonexistent `twilio_sms._send_sms` → every recipient "failed" (fails closed) |
| Inbound calling / ringing | 🟡 **P0** | 1/26 answered in 14 d; candidate fix `32814a7` passed 14/14 on staging, awaiting auth + handset |
| Voicemail | ✅ | 7 in 14 d |
| Email (transactional) | 🟠 | `EmailService` Graph→SMTP; not exercised this session |
| Email campaigns | ⬜ | send endpoints never delivered mail; `campaign_recipient` has no status columns; renderer crashed on `Contact.full_name`. Branch: controls now say "nothing was sent", renderer fixed, real unsubscribe added |
| Push / PWA notifications | 🟠 | per prior sessions VAPID present; native push creds absent |

### Marketing automation
| Feature | Status | Evidence |
|---|---|---|
| CRM behavioral automation (first inbound SMS / tag added → tag / segment) | ✅ | 178 completed executions, latest 2026-10-19 |
| General automation builder (drip, steps, triggers) | ⬜ | 0 automations in prod; "test" only simulates success; no execution engine |
| Newsletter subscriber sync (agent job) | 🟡→made inert | would undo opt-outs; never completed in prod (read nonexistent column) |

### AI
| Feature | Status | Evidence |
|---|---|---|
| Agent scheduler (19 agents, ~40 jobs) | 🟡 | every job logs "no company found" → no-ops; runs twice (ghost service) |
| OpenAI generation | ❌ (runtime) | `credit_balance_exhausted` 429 on every call |
| Human approval for AI content | 🟠 | `submit_for_approval` path exists; not exercised |

### Billing
| Feature | Status | Evidence |
|---|---|---|
| Checkout session / portal | 🔒 | code present, never used in prod |
| Webhook (signed, idempotent, 5 lifecycle events) | 🔒 | code reviewed; 3 signature failures in July only |
| Entitlement enforcement | 🟠 | seat limit on add-user; feature licenses exist |
| Sandbox testing | ❌ | no Stripe test keys anywhere |

## 3. Defect backlog

Status: **FIXED** (in branch, tested, not deployed) · **AUTH** (needs your explicit authorization) · **OPEN**

### P0 — launch blockers
| # | Defect | Status | Evidence / fix |
|---|---|---|---|
| P0-1 | **Unauthenticated account takeover**: `/auth/forgot-password` displays the reset link to the requester | FIXED in `212ae1d` (committed, not deployed) | link only emailed; generic response; older links invalidated; reset revokes sessions. Prod log check: 2 POSTs in 14 d, 0 tokens ever issued → no exploitation evidence |
| P0-2 | `/user/add-user` lets any user create a platform admin; `/user/edit-user/<id>` lets any user edit anyone | FIXED in `212ae1d` (committed, not deployed) | forwarded (307) to the guarded canonical routes; 0 hits in 14 d of logs |
| P0-3 | Self-promotion to admin via `PUT /api/user/profile {"tags":["admin"]}` (`is_admin_user` honored tags/segment) | FIXED in `212ae1d` (committed, not deployed) | `is_admin_user` = `is_admin` only; no prod user relied on tags |
| P0-4 | `/saas/*` console (all tenants' accounts, subscription tier, billing log, n8n trigger) open to any signed-in user | FIXED in `212ae1d` (committed, not deployed) | blueprint-level platform-admin gate; sidebar hidden from tenant admins |
| P0-5 | Cross-tenant IDOR: campaigns (view/edit/preview/send), automations (edit/toggle/pause/resume/delete), contact activities (+add), SMS analytics, calendar get/reschedule/delete, deal detail, blog, SEO audit, social follower refresh | FIXED in `212ae1d` (committed, not deployed) | `services/tenant_scope.py`; scanner `tools/pk_fetch_scan.py` |
| P0-6 | No working email unsubscribe (CAN-SPAM) | FIXED | `/email/unsubscribe/<signed token>` (GET confirm, POST + RFC 8058 one-click), canonical opt-out + audit |
| P0-7 | Email campaign "send" reports sent; nothing is delivered | FIXED (honest) | controls now refuse with "nothing was sent"; delivery itself is P1-1 |
| P0-8 | **No database backups** (newest full dump 41 days old, same disk) | AUTH | `deploy/backup/` script + timer + restore drill, tested on dev DB; needs install + off-site target |
| P0-9 | Prod `ENCRYPTION_MASTER_KEY` readable by 4 local accounts (0644 unit + `systemctl show`) | AUTH | `chmod 600` the unit + `.bak`; stop/disable ghost service; plan key rotation |
| P0-10 | Ghost `luxit.service` runs a duplicate Agent Scheduler on prod DB with stale code | AUTH | `systemctl disable --now luxit.service` (no nginx route, no traffic since 2026-07-18) |
| P0-11 | `weekly_contact_dedupe` re-armed by the 2026-10-17 reboot; ran 2026-10-18 (merged 0, no data change) | **CONTAINED 2026-10-19 18:00:53 UTC**: paused + hold trigger on prod (`/root/luxit-ops-backups/20261019T180044Z-dedupe-hold-prod/`, ROLLBACK.md there). Worker not restarted; Tuya unaffected. Worker upgrade still open |
| P0-12 | Inbound calls don't ring (1/26 answered in 14 d) | AUTH | candidate `32814a7` staging-accepted 14/14; needs prod auth + handset checks |
| P0-13 | No self-serve signup / email verification / trial → checkout | OPEN | Stage C build |
| P0-14 | Billing never validated; no sandbox keys | OPEN (blocked) | needs Stripe test-mode keys + webhook secret for staging |
| P0-15 | Prod SHA not on GitHub | AUTH | push branch/tag (not `main`) |

### P1 — essential launch functionality
| # | Defect | Status |
|---|---|---|
| P1-1 | Email campaign delivery pipeline (worker, recipient status schema, tracking, List-Unsubscribe header, physical address) | OPEN |
| P1-2 | Unhandled exceptions persisted half-written changes (error logger committed the request session) | FIXED |
| P1-3 | User edit always 500 (`user.role` doesn't exist); role value unvalidated; tenant admin could edit a platform admin / cross-tenant user; admin self-edit would demote; role select had no real preselection, so once the crash is fixed every plain save would demote the user to viewer | FIXED |
| P1-4 | `/user/add` open to viewers; no seat-limit check | FIXED |
| P1-5 | Deals pipeline 500 (`DealActivity` missing) | FIXED (model + migration `20261019_deal_activity.sql`, not applied anywhere) |
| P1-6 | `/crm-unified` always 500; lead scores loaded across tenants | FIXED |
| P1-7 | 7 feature models have no `company_id` (EmailTemplate, Poll, ABTest, Event, LandingPage, Influencer, AffiliateLink) → shared across tenants. Prod: all 7 tables have 0 rows; only Events is linked in the sidebar | OPEN — scope Events (migration) or unlink it; keep the other 6 unlinked until scoped |
| P1-8 | `/health/deep` + 2 market-intelligence admin routes 500 (`_ensure_admin_access` deleted) | FIXED |
| P1-9 | No login/reset rate limiting | FIXED (per-worker, keyed on `X-Real-IP`) |
| P1-10 | OpenAI account out of credits; all AI features fail | OPEN (billing/account action) |
| P1-11 | Marketing site over-claims (§1) | OPEN |
| P1-12 | SMS campaign send runs in a web-worker daemon thread (lost on recycle/restart) | OPEN |
| P1-13 | Staging lacks the prod 645dd65 fix; three diverged lines (prod / staging / dev) | OPEN |
| P1-14 | General automation builder has no execution engine (UI only) | OPEN — hide or build |
| P1-15 | No team invitation flow (admin sets other users' passwords) | OPEN |
| P1-17 | 7 of 26 inbound calls (14 d) stay `ringing` forever: no final status recorded (rows only touched by bulk updates, e.g. 2026-10-16 07:49:40). Missed-call reporting/alerts are wrong. Verify whether candidate `32814a7` covers status finalization | OPEN |
| P1-16 | Stored XSS on the app origin from tenant-authored HTML rendered `|safe` | FIXED (mitigation): public newsletter archive strips markup (regression test fails on base); template preview, email preview (JS template-literal breakout → `tojson`) and AI agent reports render in sandboxed iframes; landing-page schema via `tojson`+`JSON.parse`. EmailTemplate has no content column at all (stub feature) |

### P2
- Login form `csrf.exempt` (login CSRF); several JSON deal endpoints `csrf.exempt` (SameSite=Lax mitigates).
- `/api/marketing/sms-campaigns/<id>/send` imports missing `_send_sms`.
- `PasswordResetToken` stores raw tokens (store a hash).
- Template/route drift writes non-columns silently (`contact.last_activity`, `deal.closed_at`, `subscribed_at`…).
- `/api/social/wordpress-media` references undefined `WordPressIntegration`.
- 101 failing + 18 erroring tests on baseline (stale fixtures per prior sessions) — triage.
- RevenueCat webhook auth (prior session: vulnerable-low).
- `/newsletter-archive` 500s if any public newsletter lacks a slug; it also lists every tenant's public newsletters together on luxit.app.
- Email templates are a stub: `email_template` has only id/name/created_at; the editor writes non-columns.

### P3
- MFA, SSO, status page, AUP page, in-app support center.

## 4. Changes in this branch

| File | Change |
|---|---|
| `auth.py` | reset link + username emailed only; generic responses; invalidate older links; reset revokes sessions; rate limits |
| `templates/auth/forgot_password.html`, `forgot_username.html` | generic confirmation copy |
| `templates/auth/email_password_reset.html`, `email_forgot_username.html` | new email bodies |
| `user_management.py` | legacy add/edit forward to guarded routes |
| `routes.py` | tenant-scoped lookups (≈30 handlers); `edit_user` guards + role validation + crash fix; `add_user` permission + seat limit; `lux_crm` fix; `_ensure_admin_access` restored; honest email send |
| `models.py` | `is_admin_user` = `is_admin`; new `DealActivity` |
| `saas_mgmt.py`, `templates/base.html` | platform-admin gate; sidebar |
| `templates/edit_user.html` | real workspace role preselected; admin checkbox only for platform admins |
| `services/tenant_scope.py` | new helper |
| `services/subscriber_sync_service.py` | segment→subscriber sweep made inert |
| `services/email_unsubscribe.py`, `email_unsubscribe_public.py`, `templates/email_unsubscribe/*` | unsubscribe flow |
| `email_service.py` | real unsubscribe URL; `full_name` crash |
| `marketing_api.py` | email send = honest dry-run (501) |
| `app.py` | rollback before error logging; limiter init; unsubscribe blueprint |
| `extensions.py` | limiter |
| `migrations/20261019_deal_activity.sql` | new, additive, idempotent (NOT applied) |
| `deploy/backup/*` | backup script/timer, restore drill (NOT installed) |
| `deploy/ops/dedupe_pause.py`, `dedupe_hold_{install,release}.sql` | prod-ready pause + hold (NOT run on prod) |
| `templates/preview_email.html`, `preview_template.html`, `newsletter_archive_public.html`, `landing_page_builder.html`, `view_agent_report.html` | XSS mitigation; template preview now actually shows content (was always blank) |
| `tests/test_gtm_*.py`, `tests/test_segment_management_suppression.py` | 37 new regression tests (26 security/authz, 6 unsubscribe/send, 3 rate-limit, 1 consent, 1 XSS); email-send assertion updated to the honest contract |

## 5. Tests

Run everything network-isolated with no prod env loaded:

    cd /root/luxit-gtm-readiness
    unshare -n env -i HOME=/root PATH=/usr/bin:/bin LANG=C.UTF-8 \
      /root/lux-email-bot/.venv/bin/python -m pytest tests -q -p no:cacheprovider

- Baseline `645dd65`: **101 failed, 1080 passed, 125 skipped, 18 errors** (21 m 48 s).
- New GTM tests on base 645dd65: 21 of 25 security tests fail (they reproduce the defects); all pass on the branch.
- Branch final (all changes): **101 failed, 1117 passed, 125 skipped, 18 errors** — failure/error set identical to baseline (0 new, 0 fixed); +37 passing = the new tests.

## 6. Security posture (remaining)

- Prod is still exposed to every P0 in §3 until a deploy is authorized.
- Treat `ENCRYPTION_MASTER_KEY` as disclosed to local accounts → rotate. Verified 2026-10-19: `serveradmin` (the GitHub Actions deploy SSH user) can read it via `cat` of the unit and via `systemctl show luxit -p Environment`.
- Prod env file also holds `VPS_PASSWORD`, `GITHUB_PERSONAL_ACCESS_TOKEN`, `POSTGRES_PASSWORD` for the app process — remove what the app doesn't need.
- **Dependencies (pip-audit of the prod venv, 2026-10-19): 18 vulnerable packages, 107 advisories** — aiohttp 3.12.15 (35, fix ≥3.14.4), pillow 12.0.0 (19, ≥12.3.0), pyjwt 2.10.1 (16), cryptography 45.0.5 (7, ≥50.0.0), urllib3 2.5.0 (6, ≥2.8.0), pyasn1, soupsieve, werkzeug 3.1.3 (4, ≥3.1.9), anyio, flask 3.1.1 (≥3.1.3), pygments, protobuf, requests 2.32.4 (≥2.33.0), idna, lxml, oauthlib, pytest, python-dotenv. P1: upgrade in a branch + full regression run.
- Stored XSS: see P1-16.
- Unverified this session: SSRF (integration URL fields), file upload validation, secrets in logs beyond SMS masking.
- Compliance (for legal review, not asserted): TCPA consent records for SMS marketing, CAN-SPAM physical address in campaign emails, call-recording consent per state, Terms clauses ("Data may be deleted without notice", unilateral changes), no DPA/subprocessor list, no AUP.

## 7. Actions that need your authorization (ordered by risk reduction)

1. ~~Re-pause `weekly_contact_dedupe` + hold~~ done 2026-10-19.
2. Deploy this branch's security fixes to staging → acceptance → prod (new detached dir + drop-in, as for 645dd65).
3. `systemctl disable --now luxit.service`; `chmod 600 /etc/systemd/system/luxit.service*`; plan key rotation.
4. Install the backup timer, run first backup + restore drill; pick an off-site target.
5. Push the prod lineage + this branch to GitHub (feature branch/tag only).
6. Phone fix `32814a7` → prod (with handset checks).
7. Provide Stripe test-mode keys + webhook secret for staging; top up or replace the OpenAI key.

## 8. Run log

- 2026-10-19 18:00 UTC: dedupe hold applied on prod (authorized). Commits on branch: `212ae1d` (P0 containment, release candidate), `49b40ff` (hardening), then correctness/compliance + docs. Exploitation evidence: `SECURITY_INCIDENT_EVIDENCE_20261019.md`. Staging plan: `STAGING_PLAN_SECURITY_212ae1d.md`.

- 2026-10-19: dedupe pause+hold drill PASS on a disposable copy of the prod jobstore (control re-arms, hold holds).
- 2026-10-19: Discovery; P0-1…P0-7, P1-2…P1-9 fixed in branch; restore drill PASS (2026-09-08 dump, 4 s); backup scripts tested on dev DB.
