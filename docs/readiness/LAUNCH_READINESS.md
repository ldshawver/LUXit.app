# LUXit.app — Launch Readiness Tracker

Living document and the single resume point for SaaS go-to-market work.
Another session must be able to resume from this file alone. Update the
"As of" line, the tracker table and the run log whenever a gate moves.

- **As of:** 2026-10-27 06:30 UTC (audit re-verified read-only; T-26 added; §9 = current go/no-go matrix)
- **Branch:** `saas/gtm-launch-20261023`, worktree `/root/luxit-saas-launch`, based on **prod `1c56fbe`**. Work is in **focused local commits** (local commits authorized 2026-10-26; nothing pushed or deployed). See §8 for the commit list.
- **Supersedes:** `saas/gtm-readiness-20261019` (`/root/luxit-gtm-readiness`, based on 645dd65). Its security commits 212ae1d/49b40ff reached prod via 09de17d → d0025bb → 1c56fbe; its third commit `d3523f0` (unsubscribe, DealActivity, honest email send, backups, ops) never did and is carried into this branch.
- **Scope boundary:** LUXit.app SaaS platform only. LUX Connect / LUXit Phone native client (`/root/luxit-connect-native*`, `/api/mobile/v1`, integration `00f5fa0`) is a separate product and release train; it does not block SaaS gates except where marked.
- **Verdict:** **NO-GO.** See §2.

---

## 0. Environment (verified read-only 2026-10-23 23:25 UTC)

| Thing | Value |
|---|---|
| Host | single VPS, up since 2026-10-17 04:06 UTC (6 d 19 h). ~1.3 GB RAM available → run one pytest suite at a time under `systemd-run --scope -p MemoryMax=1G`. |
| **PROD web** | `lux-email-bot.service` PID 3415576, NRestarts 0, since 2026-10-22 11:25:53 UTC; cwd `/root/luxit-prod-1c56fbe-20261022` = **`1c56fbe`**, clean; last drop-in `zzz-release-1c56fbe-20261022.conf`. `https://luxit.app/health` 200. |
| **PROD worker** | `luxit-tuya-notification-worker.service` PID 1238 since 2026-10-17 04:06:12, NRestarts 0; cwd `/root/luxit-main-canonicalize` @ **`eea5b84`** (1 dirty file), stale code. |
| Legacy web | `luxit.service` PID 1871, `/root/lux-email-bot` @ `ea388ff` (5 dirty), 127.0.0.1:8000, not proxied; runs a duplicate in-memory agent scheduler on prod DB. Its venv is prod's ExecStart venv — never delete it. |
| Staging | `lux-email-bot-staging.service` PID 1936879 since 2026-10-20 04:36; `/srv/lux-email-bot-staging-fwd-9311cec` (`9311cec`, forwarding safety); DB `luxstaging` :5434. **Holds the PROD Twilio account credentials.** `https://staging.luxit.app/health` 200. |
| Dev | `lux-email-bot-dev.service` PID 1165, `/srv/lux-email-bot-dev` @ `f71f68d`; DB `luxdb_dev` :5433. 200. |
| nginx containment | prod: v1 `/chatbot/send` + v3/v4 P0 system-route snippet. dev+staging: P0 snippet + `luxit-nonprod-p0.conf`. staging: push-token storm snippet (sha 8c91257e…) **applied** 2026-10-22 17:55:27. |
| Push-token storm | **Stopped.** Last request 2026-10-22 23:58:26 UTC (503 at nginx); 0 today. Containment still in place (blocks push-token registration for all staging clients). |
| Scheduler | `weekly_contact_dedupe` next_run_time **NULL**, hold trigger `luxit_ops_hold_weekly_contact_dedupe` enabled (`O`); no dedupe log lines since 2026-10-19; Tuya job advancing. **Paused as intended.** |
| GitHub | `origin/main` = `4bb5d30`, lacks prod security lineage (see `/root/luxit-pr-reconcile` f6560dc). Repo is public. |

**October 17 restart:** the host reboot restarted the worker, which re-armed `weekly_contact_dedupe` (`init_scheduler` re-adds jobs with `replace_existing=True`). It ran 2026-10-18 03:00 with merged=0 (no data change, verified 2026-10-19). Re-contained 2026-10-19 18:00:53 with pause + DB hold trigger. Root cause is fixed in this branch (T-17 below).

## 1. Authoritative tracker

Status: **Verified** (tested + deployed + checked live) · **Done (branch)** (implemented + tested, not deployed) · **In progress** · **Not started** · **Blocked**

| # | Feature | Current state | Evidence | Sev | Dependencies | Required changes | Acceptance criteria | Status |
|---|---|---|---|---|---|---|---|---|
| T-1 | Password reset / forgot username | Working | 212ae1d in prod; tests `test_gtm_p0_security` | — | email delivery | — | link only emailed, generic response | Verified (prod) |
| T-2 | Privilege / IDOR / `/saas` / AI-repair lockdown | Working | 212ae1d…1c56fbe in prod; nginx v1/v3/v4 | — | — | retire legacy luxit.service | security suites pass; 0 cross-tenant hits | Verified (prod) |
| T-3 | **Self-serve signup + org creation** | Missing in prod | `services/signup.py`, `auth.py` `/auth/signup`; 12 tests | P0 | migration `20261023_user_email_verification.sql`; email delivery; **T-16** | flag `SELF_SERVE_SIGNUP_ENABLED` (off by default) | signup → org + owner, never platform admin; no enumeration; honeypot; rate limit 5/h | Done (branch) |
| T-4 | **Email verification** | Missing in prod | `/auth/verify-email/<token>`, resend w/ 2-min cooldown; login gate; reset counts as verification | P0 | same as T-3 | — | unverified signup can't log in; token bound to email, 48 h; legacy accounts ungated | Done (branch) |
| T-5 | Team invitations | Missing in prod (admins set passwords directly) | `services/invitations.py`, `/user/invite`, `/user/invite/<id>/revoke`, `/auth/invite/<token>`; Team page invite form + pending list; `test_gtm_team_invitations` 9 tests | P1 | migration `20261026_team_invitation.sql`; email delivery | — | invitee sets own password or signs in; role ≤ inviter's; seats incl. pending invites; single-use, 7-day, hash-only tokens; wrong account → 403; other tenant can't revoke | Done (branch) |
| T-6 | Plan selection → checkout → activation | Code present, never exercised | `services/stripe_billing.py`, `/app/billing`, webhook | P0 | **Stripe test-mode keys + webhook secret on staging** | sandbox run | test card → `billing_status=active`, tier + seats applied, idempotent replay | Blocked (keys) |
| T-7 | Billing authorization | **Broken in prod**: any member (viewer) could open portal / checkout / usage report | `saas_mgmt._user_can_manage_billing`; `test_gtm_billing_authz` 4/6 fail on 1c56fbe, 6/6 pass | P1 | — | owner/admin only | viewer/manager/non-member → 403 | Done (branch) |
| T-8 | Entitlements without a subscription | Defined + enforced for self-serve workspaces only (`company_plan_policy` row); pre-SaaS tenants unrestricted | `services/entitlements.py`: free = 1 seat, 250 contacts (enforced in `resolve_or_create_contact`), no AI (enforced in every OpenAI `_get_client`), no phone/SMS (license never granted by a plan); `PlanLimitReached` → 402 JSON / redirect to billing; `test_gtm_free_plan_entitlements` | P0 for signup | migration `20261026_company_plan_policy.sql` | — | free org: contact cap, AI 402, no SMS; paid/trialing lifts limits; legacy tenants unaffected | Done (branch) |
| T-9 | Contacts / segments / audiences | Working | prod usage; segment-10 work | — | — | — | — | Verified (prod) |
| T-10 | Deals pipeline | Broken in prod (500, `DealActivity` missing) | d3523f0 carried | P1 | migration `20261019_deal_activity.sql` | — | deal pages 200 | Done (branch) |
| T-11 | CSV import / export | **Export fail-open (prod 1c56fbe):** a user without a default company got every tenant's contacts; any member (viewer) could export/import. Export has only Email/First/Last (no phone, tags or consent state) | `routes.py export_contacts/import_contacts`; `test_gtm_contact_export_scope` | P1 | — | scope via `current_company_id()`, refuse without a workspace, require `can_edit_company`; later: phone/tags/consent columns | editor gets own tenant only; viewer/companyless → redirect, nothing written | Done (branch, scoping) / Not started (columns) |
| T-12 | Email unsubscribe (CAN-SPAM) | Missing in prod | d3523f0 carried; `/email/unsubscribe/<token>` | P0 for email marketing | — | — | one-click + audit row | Done (branch) |
| T-13 | Email campaign delivery | No delivery path; branch makes send honest (501) | d3523f0 carried | P1 | worker, recipient status schema | build or exclude from launch | real send via queue, per-recipient status | Not started (exclude from launch) |
| T-14 | SMS two-way + campaigns | Working (SMS); campaign sends in web daemon thread | prod 5.7k msgs | P1 | — | move sends to worker | survives restart | Not started |
| T-15 | Calling (inbound ring) | 1/26 answered (10-19); candidates 32814a7 / 3099ec8 | phone memories | P1 (P0 if advertised) | handset tests | — | ring + answer on handset | Blocked (handset + auth) |
| T-16 | **Tenant sender isolation** | Outbound SMS falls back to the **platform** Twilio account/number when a tenant has no own config. Mitigation found: `services/sms_service.send_sms` + campaign + `twilio_sms.py:730` paths require an active `phone_pwa` TenantLicense, which a new org does not get (`ensure_company_default_license` only seeds company 1 / an empty table). Legacy root `sms_service.py` has no callers (dead). Audited 2026-10-23: `twilio_sms` conversation send (tenant TwilioAccount + license, fail-closed), `inbox_pwa` outbound calls (tenant TwilioAccount), `services/sms_service` (license, then **platform fallback** if the tenant has no config), `integrations_bp /api/twilio/send-sms` (platform, no gate → fixed under T-25). 2026-10-26 fix: `SMSService.send_sms` refuses a missing `company_id` (403) and refuses the platform fallback for any tenant not in `LUXIT_PLATFORM_SENDER_COMPANY_IDS` (default `1`) (409 `sender_missing`); Stripe events no longer *create* a phone/PWA license (only keep an existing one in step); `/api/phone/voice-token` (always minted on the platform account) is limited to platform workspaces. | `services/sms_service.py`, `services/license_service.py`, `inbox_pwa.py`; `test_gtm_tenant_sender_isolation` 8 tests | **P0 before enabling signup** | **pre-deploy check:** list prod companies with an active `phone_pwa` license / PWA voice use other than company 1 and add them to `LUXIT_PLATFORM_SENDER_COMPANY_IDS` (prod DB read was not permitted in this session) | — | new org: SMS 402/409, voice token 403; company 1 unchanged | Done (branch) |
| T-25 | **Platform-owned integration accounts exposed to tenants** | **Live on prod 1c56fbe**: any signed-in user of any tenant (viewer included) could `POST /api/twilio/send-sms` (platform Twilio number, any recipient; consent checked only if the number is already a contact of that tenant), use `/api/outlook/*` (send as the platform Microsoft 365 mailbox, list its inbox subjects/senders, read/create its calendar — **MS Graph is configured on prod**) and `/api/airtable/*` (platform Airtable base; not configured on prod). No nginx block. 0 hits in nginx logs 2026-10-09 → 10-23 (all retained logs). Legacy `/api/twilio/inbound-sms` was unsigned but is unreachable (blueprint not CSRF-exempt → 400). | `integrations_bp.py`; `test_gtm_platform_integrations_authz` | **P0** (P1 at today's 7 trusted users) | — | 16 routes → `_require_platform_admin()`; inbound webhook requires Twilio signature, reply XML-escaped | tenant owner → 403 on all 16; no send function called | Done (branch) |
| T-26 | **Security audit trail** | Missing in prod: no record of who changed a role/admin flag, archived a user, invited/revoked, opened billing, or edited a tenant/license from `/saas` (`ActivityLog` is a user feed; `SaasAutomationLog` has no actor). Login also logged password length | `models.SecurityAuditEvent`, `services/security_audit.py` (`record()` after commit, savepoint, never raises; allowlisted detail; no typed identifier/password/token/email stored), `GET /api/security/audit-events` (own workspace for owner/admin, any for platform admin); hooks in `auth.py`, `routes.py` user/invite routes, `saas_mgmt.py`; `pw_len` log removed; `tests/test_gtm_security_audit.py` 8 tests | P1 (P0 for multi-tenant launch evidence) | migration `20261027_security_audit_event.sql` (append-only trigger; verified on a throwaway PG17: applies twice, UPDATE/DELETE/TRUNCATE rejected) | retention period = decision D-9 | events for sign-in/role/invite/archive/billing/platform edits; tenant-scoped read; broken table never blocks the action | Done (branch) |
| T-17 | **Scheduler pause survives restart** | Broken in prod worker (re-arm on start); contained by DB trigger | `scheduler._paused_job_ids`; `test_scheduler_pause_survives_restart` 3/5 fail on 1c56fbe, 5/5 pass | P0 | worker upgrade | — | paused job stays NULL across 3 restarts; read failure aborts startup | Done (branch) |
| T-18 | Worker on current code | Worker runs eea5b84 | §0 | P1 | auth to restart worker | Strategy B (detached dir + drop-in, hold stays) | worker cwd = release SHA, dedupe still NULL, Tuya advancing | Not started (needs auth) |
| T-19 | Backups | No scheduled backups; manual pre-deploy dumps only | `deploy/backup/` carried | P0 | off-site target | install timer | nightly dump + monthly restore drill | Blocked (auth + target) |
| T-20 | AI features | OpenAI out of credit (429) | 10-22 journal | P1 | account funding | then audit tenant scoping per Phase 4 | advertised AI works, scoped, metered | Blocked (account) |
| T-21 | Marketing site accuracy | Over-claims (10-19 list) | — | P1 | — | copy changes | every claim maps to a working feature | Not started |
| T-22 | Legal pages | Terms + Privacy + SMS consent exist (updated 2026-04-26); **no AUP**; no billing/cancellation disclosure | `legal.py` | P1 | **professional legal review** | AUP, refund/cancel terms | reviewed by counsel | Not started |
| T-23 | Models without `company_id` (7) | Shared across tenants; tables empty | 10-19 audit | P1 | migration | scope or keep unlinked | no cross-tenant read | Not started |
| T-24 | Dependency CVEs | 18 pkgs / 107 advisories (10-19) | pip-audit | P1 | full regression | upgrade branch | pip-audit clean of high | Not started |

## 2. Go / no-go (Phase 11 scorecard)

| Gate | Result | Why |
|---|---|---|
| Authentication | **FAIL** | signup/verification built (branch) but not deployed; no invitations; no MFA |
| Tenant isolation | **PASS (scoped) / FAIL (platform channels)** | security suites pass on prod 1c56fbe; T-16 platform-sender fallback (license-gated on audited paths); T-23 |
| CRM | **FAIL** | deals 500 in prod; import/export unverified |
| Communications | **FAIL** | inbound calling; email campaigns have no delivery path |
| Marketing automation | **FAIL** | general builder has no execution engine; SMS sends in web thread |
| Billing | **BLOCKED** | no Stripe test keys anywhere; never exercised |
| Onboarding | **FAIL** | signup flag off; entitlements for unpaid orgs undefined (T-8) |
| AI | **BLOCKED** | OpenAI out of credit |
| Background jobs | **FAIL** | worker stale; re-arm fix in branch only (contained by trigger) |
| Security | **FAIL** | T-16; dependency CVEs; legacy luxit.service runs pre-security code |
| Operations | **FAIL** | no scheduled/off-site backups; no alerting |
| Support | **FAIL** | support contact is a personal address hard-coded in auth templates |
| Legal | **FAIL** | no AUP; compliance items need counsel (§5) |
| Staging | **NOT STARTED** | for this branch |
| Rollback | **PASS** | per-release detached dirs + drop-ins, exercised 10-20/10-22 |

## 3. Defect backlog carried from 2026-10-19 (status updated)

P0-1…P0-5 (takeover, priv-esc, self-promotion, `/saas`, IDORs): **deployed** (212ae1d lineage, prod 1c56fbe).
P0-6 unsubscribe, P0-7 honest email send: **in this branch** (from d3523f0), not deployed.
P0-8 backups: open (T-19). P0-9 ENCRYPTION_MASTER_KEY readable via legacy unit: open. P0-10 legacy luxit.service: open (contained to loopback). P0-11 dedupe: contained + root-cause fixed in branch (T-17). P0-12 calling: T-15. P0-13 signup: T-3/T-4. P0-14 billing: T-6. P0-15 prod SHA not on GitHub: open.
P1-1 email delivery: T-13. P1-5 deals, P1-8 `_ensure_admin_access`: in this branch. P1-2/3/4/6/9/16: deployed. P1-7: T-23. P1-10: T-20. P1-11: T-21. P1-12: T-14. P1-14 automation builder: open. P1-15 invitations: T-5. P1-17 calls stuck `ringing`: open.

New this session:
- **T-16 (P0 for signup):** tenants without their own Twilio config fall back to the platform account; license gate mitigates on the audited paths, full outbound audit pending.
- **T-7 (P1):** billing endpoints allowed any member.
- **P2:** login logs password length (`auth.py` "LOGIN: password check … pw_len"); support address `luke@adiken.com` hard-coded in forgot-password/username templates — replace with a support alias.

## 4. Changes in this branch (uncommitted)

| File | Change |
|---|---|
| *(all of d3523f0)* | unsubscribe flow, honest email send, DealActivity + migration, `_ensure_admin_access`, subscriber-sync made inert, `deploy/backup/*`, `deploy/ops/*`, readiness docs |
| `scheduler.py` | `_paused_job_ids()`; recurring jobs paused in the jobstore are re-added with `next_run_time=None`; unreadable jobstore aborts startup |
| `models.py` | `UserEmailVerification` (separate table, so loading `User` never depends on the migration) |
| `migrations/20261023_user_email_verification.sql` | new, additive, idempotent (NOT applied anywhere) |
| `services/signup.py` | flag, validation, account+org+owner creation, signed verification tokens, cooldown |
| `auth.py` | `/auth/signup`, `/auth/verify-email/<token>`, `/auth/verify-email/resend`; login gate; reset marks verified; `self_serve_signup_enabled` context flag |
| `templates/auth/_signup_base.html`, `signup.html`, `signup_sent.html`, `verify_result.html`, `email_verify.html`, `email_signup_existing.html` | new pages/emails (existing auth styling) |
| `templates/auth/login.html` | "Create an account" link when the flag is on |
| `integrations_bp.py` | 16 platform-account routes platform-admin only; legacy inbound webhook signature-checked + reply escaped |
| `tests/test_gtm_platform_integrations_authz.py` | 17 tests |
| `routes.py` (export/import) | tenant-scoped, edit rights required |
| `tests/test_gtm_contact_export_scope.py` | 5 tests |
| `saas_mgmt.py` | `_user_can_access_company` → `_user_can_manage_billing` (owner/admin or platform admin) |
| `tests/test_scheduler_pause_survives_restart.py` | 5 tests (3 fail on 1c56fbe) |
| `tests/test_gtm_self_serve_signup.py` | 12 tests |
| `tests/test_gtm_billing_authz.py` | 6 tests (4 fail on 1c56fbe) |
| `tests/test_scheduler_pause_survives_restart.py` (2026-10-26) | `boot` fixture runs `db.create_all()`: `test_market_intelligence.py` `drop_all()`s the shared app DB and `init_scheduler` queries `campaign`. 9/9 with market-intelligence in either order |
| `services/sms_service.py`, `services/license_service.py`, `inbox_pwa.py` (T-16) | no platform-sender fallback outside `LUXIT_PLATFORM_SENDER_COMPANY_IDS`; Stripe never creates a phone license; voice token platform-only; `tests/test_gtm_tenant_sender_isolation.py` 8 tests |
| `services/entitlements.py`, `models.CompanyPlanPolicy`, `app.py`, `ai_agent.py`, `agents/base_agent.py`, `services/contact_resolver.py`, `routes.py` (T-8) | free-plan limits for self-serve workspaces; 402 handler; `tests/test_gtm_free_plan_entitlements.py` |
| `migrations/20261026_company_plan_policy.sql`, `migrations/20261026_team_invitation.sql` | new, additive, idempotent (NOT applied anywhere) |
| `services/invitations.py`, `models.TeamInvitation`, `auth.py` `/auth/invite/<token>`, `routes.py` `/user/invite*`, `templates/manage_users.html`, `templates/auth/invite_*.html`, `email_invite.html` (T-5) | `tests/test_gtm_team_invitations.py` 9 tests |

**Deploy order when authorized:** apply `20261019_deal_activity.sql`, `20261023_user_email_verification.sql`, `20261026_company_plan_policy.sql`, `20261026_team_invitation.sql`, `20261027_security_audit_event.sql` via the canonical runner first, then code. Before code: set `LUXIT_PLATFORM_SENDER_COMPANY_IDS` to every prod company that legitimately sends through / calls on the platform Twilio account (default `1`). Code without the verification table still serves logins (gate fails open, logs an error) but `/auth/signup` would error. Keep `SELF_SERVE_SIGNUP_ENABLED` unset until T-8 and T-16 are done.

## 5. Security and compliance notes (for professional review, not asserted)

- TCPA: consent records for SMS marketing; per-tenant sender ownership (T-16); 10DLC brand/campaign registration per tenant before tenants send.
- CAN-SPAM: physical address in campaign emails; unsubscribe (T-12).
- Call recording consent per state, if recording is offered.
- Terms: "data may be deleted without notice" and unilateral-change clauses; no DPA/subprocessor list; no AUP; billing/cancellation/refund disclosure.
- Treat `ENCRYPTION_MASTER_KEY` as disclosed to local accounts → rotate.

## 6. Tests

Run network-isolated, no prod env, one suite at a time:

    cd /root/luxit-saas-launch
    systemd-run --scope -q -p MemoryMax=1200M unshare -n env -i HOME=/root PATH=/usr/bin:/bin LANG=C.UTF-8 \
      DIAGNOSTICS_LOG_DIR=<scratch dir> /root/lux-email-bot/.venv/bin/python -m pytest tests -q -p no:cacheprovider

Baseline failing set for 1c56fbe == d0025bb: `/root/luxit-ops-backups/20261020-ai-authz-rehearsal/HANDOVER-root-03/failures-d0025bb-recorded.txt` (95F/18E).

## 7. Authorizations needed (ordered by risk reduction)

1. **Optional immediate containment for T-25 on prod**: nginx `return 403` for `/api/twilio/send-sms`, `/api/outlook/`, `/api/airtable/` (no UI caller outside platform-admin pages; 0 hits since 10-09). Same pattern as the v3 snippet.
2. ~~Commit this branch (local only).~~ Done 2026-10-26. Push to GitHub needs separate approval.
3. Stripe **test-mode** secret key + webhook secret for an isolated staging instance (T-6).
4. Staging acceptance of this branch on an isolated instance (pattern: :8006, fresh keys, IPAddressDeny) incl. both migrations on a luxstaging copy.
5. Worker upgrade (T-18) with the hold trigger left in place.
6. Backup timer + off-site target (T-19).
7. Retire legacy `luxit.service`; chmod 600 its unit; plan key rotation.
8. Decide per-tenant Twilio model (T-16) — product decision.
9. Fund or replace OpenAI key (T-20).

## 8. Run log

- 2026-10-27 05:20–06:30 UTC: read-only re-audit (§9). Prod 1c56fbe PID 3415576 NRestarts 0 (drop-in zzz-release-1c56fbe-20261022); worker eea5b84; staging 9311cec; dev /srv/lux-email-bot-dev; all /health 200. origin/main 4bb5d30; open PRs #232/#233/#238 (no CI on PR heads; last Actions runs = Pages/Codespaces on main 2026-10-22). Implemented T-26 (security audit trail) + removed pw_len log. Tests: new suite 8/8; 30 related files (all test_gtm_*, test_stripe_*, feedback, license, every test hitting login/user-edit/saas routes) 347P/21F — 15 F are in the recorded 1c56fbe baseline, 6 test_startup F were an env artifact (`python` not on PATH) and pass 6/6 with the venv on PATH → **0 new failures**. ruff F/E9/B: 0 new findings vs HEAD; bandit (≥medium) clean on new module. Full batched suite NOT rerun (host ~0.5 GB available). Nothing pushed/deployed; no external state changed.

- 2026-10-26 09:40–10:45 UTC: recovered disconnected sessions (9e3471d7 had added T-16/T-8 and a half-built T-5 after the 10-23 entry; nothing lost). Fixed scheduler test isolation; finished + tested T-5; free-plan lookup fails open if its table is missing; split migration 20261026 into two files. Full suite in 13 memory-limited batches: **74F/17E, identical failure IDs to the 1c56fbe baseline (0 new)**; two extra failures seen only in an ad-hoc file order reproduce identically on 1c56fbe (pre-existing order dependence in test_pwa_communications_completion). Local commits on `saas/gtm-launch-20261023` (not pushed): 2bbcede carry d3523f0 · acee917 T-17 · b346a82 T-25 · 1a698f1 T-7 · 40e730d T-11 · onboarding T-3/T-4/T-8 · T-16 · T-5 · this tracker. Prod 1c56fbe / staging 9311cec / dev f71f68d / worker eea5b84 unchanged.
- 2026-10-23 23:25–23:50 UTC: state recovered read-only (§0). New branch off prod 1c56fbe + d3523f0. Implemented T-17, T-3/T-4, T-7, T-25 (platform integration accounts exposed to tenants; live on prod; nginx containment candidate). Found T-16 (platform-sender fallback; license gate mitigates on audited paths; legacy root sms_service.py is dead code).
- 2026-10-22: 1c56fbe deployed to prod; non-prod containment; staging storm containment.
- 2026-10-20: 09de17d deployed to prod; chatbot containment.
- 2026-10-19: dedupe pause + hold applied on prod; discovery; P0 security fixes.

## 9. 2026-10-27 audit — leads verified, go/no-go matrix

### 9.1 Leads (treated as unverified until checked)

| Lead | Finding (evidence) | Confidence |
|---|---|---|
| Hard-coded trial duration | **No trial anywhere in LUXit code.** Checkout (`services/stripe_billing.create_checkout_session`) sets no `trial_period_days`; pricing page + `/app/billing` advertise no trial; `trialing` is only *mirrored* from Stripe (`entitlements.PAID_BILLING_STATUSES`, `license_service.ACTIVE_STATUSES`). `TenantLicense.trial_ends_at` column exists, nothing sets it. What *is* hard-coded is the **14-day payment grace** (`BILLING_GRACE_DAYS` default 14) and free plan = 1 seat / 250 contacts. | High |
| Missing Stripe setup | Prod `/health`: `stripe=configured` (secret + webhook secret present; 10-19 audit: `sk_live`, 0 webhook events, 0 customers — not re-checked, Stripe dashboard not accessed). Staging/dev: **no keys** (`/ready` 503 on staging). Lookup keys required in Stripe: `luxit_starter_monthly`, `luxit_professional_monthly`, `luxit_additional_account_monthly`, `luxit_contacts_usage_monthly`, `luxit_setup_fee_once` — existence in the Stripe account unverified. Page prices ($199 / $499) are template text, not read from Stripe → can drift. | High (config present) / Unknown (prices exist) |
| Incomplete contracts | **No LUXit customer contract / order form / e-signature flow.** `services/documenso_service.py` exists but serves the separate PayLink contract flow (docs/paylink_documenso_production_signing_scope.md); no LUXit route calls it. Customers accept web Terms only. | High |
| Public demo credentials | **Not found**: no demo user/password in repo (excluding tests) or on live `/`, `/auth/login`, `/pricing`, `/book-demo`. | Medium (public pages only) |
| Backup gaps | **Confirmed.** No backup timer/cron (only `dpkg-db-backup`); newest DB dump = pre-deploy `luxit-before-1c56fbe-20261022T112013Z.dump`, same host, no off-site copy. `deploy/backup/` unit is in branch, not installed. | High |
| Monitoring | **None** beyond `/health`: no alerting/uptime/error-tracking service running; Sentry not configured. | High |
| Audit logging | Was missing for privileged actions → fixed in branch (T-26). | High |
| Support | No support desk; `support@luxit.app` on contact/SMS pages (mailbox status unverified); auth emails hard-code a personal address. In-app feedback tickets exist. | Medium |
| Legal pages | `/terms` `/privacy` `/sms-consent` 200; `/acceptable-use` `/refund-policy` `/dpa` `/subprocessors` 404. | High |
| Self-serve signup | Live `/auth/signup` 404 (prod lacks branch); branch has it flag-gated off. | High |
| On-prem licensing | No on-prem/offline license mechanism; product is hosted-only (`TenantLicense` = per-tenant feature modules). Only a gap if on-prem is sold — decision D-8. | High |

### 9.2 Launch blockers (must have evidence before GO)

| # | Blocker | Evidence |
|---|---|---|
| B-1 | Branch not deployed: signup, verification, invitations, sender isolation (T-16), platform-integration lockdown (T-25, **live on prod**), billing authz (T-7), export scoping (T-11), audit trail (T-26) | prod = 1c56fbe; branch = 4727798 + T-26 |
| B-2 | Billing never exercised end-to-end | no Stripe test keys on any non-prod instance; 0 prod webhook events (10-19) |
| B-3 | No scheduled / off-site backups or restore drill on schedule | §9.1 |
| B-4 | No alerting | §9.1 |
| B-5 | Commercial terms not decided/published: pricing in Stripe, refund/cancel, AUP, DPA/subprocessors, customer agreement | §9.1, D-1…D-7 |
| B-6 | Worker on stale code (eea5b84); legacy `luxit.service` with readable `ENCRYPTION_MASTER_KEY` | §0, §3 |
| B-7 | Per-tenant messaging model (Twilio subaccount/10DLC) undecided → SMS/voice can't be sold to new tenants | T-16, D-6 |

Non-blocking risks: dependency CVEs (T-24); email campaigns have no delivery path (exclude from launch, T-13); SMS campaigns in web thread (T-14); OpenAI out of credit (T-20, sell without AI or fund); marketing over-claims (T-21); no MFA; 7 unscoped models (empty tables, T-23); calling reliability (T-15, only if advertised); legacy `/saas` license edits not validated against Stripe.

### 9.3 Decisions needed from the owner (business / legal / finance)

- D-1 Trial: none, or N days, card-required or not (code currently: no trial; free plan exists for self-serve).
- D-2 Prices + setup fee amount, and confirmation that the 5 Stripe lookup keys exist in the live account.
- D-3 Payment-failure grace: keep 14 days? What access during grace and after suspension?
- D-4 Refund / cancellation / proration policy (needed for Terms + checkout disclosure).
- D-5 Customer agreement model: click-through Terms only, or signed order form/MSA (and with which e-sign tool) for Professional/Enterprise.
- D-6 Messaging model for tenants: bring-your-own Twilio vs platform subaccounts; who registers 10DLC; who pays usage.
- D-7 Legal pages: AUP, DPA, subprocessor list, retention — counsel review.
- D-8 On-prem / self-hosted licensing: in scope or hosted-only?
- D-9 Security-audit retention period (table is append-only; purge needs an explicit job).
- D-10 Off-site backup target + RPO/RTO; alerting channel (email/SMS/pager) and on-call owner.
- D-11 Support channel: confirm `support@luxit.app` is a monitored mailbox; response-time commitment.

### 9.4 Smallest sequenced plan

1. **Staging acceptance of this branch** (isolated :8006 pattern, fresh keys, IPAddressDeny, luxstaging copy, all 5 migrations). *Accept:* security/acceptance script green; signup→verify→invite→role change visible in `/api/security/audit-events`; viewer 403 on billing/T-25 routes; tenant without Twilio → SMS 409 / voice 403. *Tests:* full batched suite 0 new vs 1c56fbe; `sec_acceptance.py` extended for T-25/T-26.
2. **Billing sandbox** (needs Stripe test keys in the secret manager + D-1…D-4). *Accept:* test card → `billing_status=active`, tier/seats applied; replayed webhook idempotent; failed invoice → grace → suspended; cancel at period end; viewer cannot open portal. *Tests:* existing 31 Stripe tests + a scripted Stripe CLI event run.
3. **Ops baseline** (D-10): install backup timer with off-site copy, monthly restore drill, uptime + 5xx alerting. *Accept:* nightly dump present off-host for 3 nights; restore drill log; alert fires on a staged `/health` failure.
4. **Prod release** (auth required): migrations via canonical runner → `LUXIT_PLATFORM_SENDER_COMPANY_IDS` set from a read-only company audit → code via detached dir + `zzz-` drop-in; worker upgrade (T-18) keeping the hold trigger; retire legacy `luxit.service` + key rotation plan. *Accept:* smoke + signed-in human checks; signup flag still OFF.
5. **Commercial go-live** (D-1…D-7, D-11): publish legal pages, align pricing page with Stripe, enable `SELF_SERVE_SIGNUP_ENABLED`. *Accept:* every launch claim maps to a verified feature (T-21); first real customer checkout observed in webhook log.
