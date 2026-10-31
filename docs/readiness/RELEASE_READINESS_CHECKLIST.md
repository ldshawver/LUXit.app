# Release readiness checklist: SaaS + native release candidate

Live checklist for branch `release/saas-native-20261029`. Each line has
objective evidence or names exactly what is missing. Local commits only:
nothing pushed or deployed. Production and staging have not been touched.

Legend: **PASS** (evidence below) · **READY** (built and validated, needs approval to install/run) ·
**BLOCKED** (external credential or owner decision) · **OPEN**

## A. Release and security stability

| Item | State | Evidence |
|---|---|---|
| Full regression b00–b15 | PASS (0 new) | 2026-10-31, code b9752aa, all 135 files, one pytest process per file (MemoryMax 900M, no network): 1674 passed / 68 failed / 125 skipped; all 68 failures are in the 1c56fbe-lineage baseline (`rc-20261031-failures-full.txt`). New tests fail without their fix: 4 billing/journey tests (negative control) |
| Merge fixes (T-16 gate shape, native sign-in verification/audit) | PASS | 8b483e6, b5ef14a; native API suite |
| Password change/reset revokes other sessions (web) | PASS | efdf732; `test_password_reset_revokes_existing_sessions`; journey step 19 (second device signed out, old password refused) |
| Revoked sessions stop native ringing / tokens | PASS | `ringable_devices` honors `session_revoked_at`; `test_principal_changes_take_effect_on_next_request`, `test_revoked_session_access_token_is_rejected`, handset tests |
| Cross-tenant isolation | PASS | PK-fetch scan (no new unscoped fetch vs prod); journey stranger checks; Stripe tenant-redirect test; T-16/T-25/T-29 suites |
| Worker compatibility | PASS (code) | the three persisted job functions the worker (eea5b84) schedules (`run_scheduled_campaign`, `run_tuya_reconciliation`, `run_weekly_contact_dedupe`) still exist at module level in the RC; `test_scheduler_pause_survives_restart`. Reading prod's `apscheduler_jobs` was not permitted, so this check reads code, not stored rows |
| Migration ordering | PASS | staging ledger 42 rows, 0 drift; 8 pending, all `CREATE … IF NOT EXISTS` (7 previous + `20261031_billing_event_cursor`); deploy guard rejects ALTER/DROP/DELETE/UPDATE |
| Rollback | READY | staging: remove the `zzz-release-rc-*` drop-in → 9311cec; the new tables are ignored by older code |

## B. Operational readiness

| Item | State | Evidence |
|---|---|---|
| Encrypted scheduled backups | READY | `deploy/backup/` (9df462c): pg_dump → TOC check → gpg to a public key (refuses to run unencrypted) → status file. Run against staging: 2144 TOC entries, 0.9 MB, 1 s |
| Off-site copy | BLOCKED (D-10 bucket + credentials) | `luxit-backup-offsite.sh` (rclone `--immutable`, size-verified, refuses plaintext). The health check reports `offsite=no` as a failure until it is configured |
| Restore drill (isolated) | PASS | throwaway initdb cluster with no TCP listener: 207 tables / 42 migrations / 15 companies restored in 5 s; a tampered file, a drill with only the public key, and a missing recipient all fail |
| Health checks + alerting | READY; channel BLOCKED (D-10) | `deploy/monitoring/luxit_healthcheck.py` (/health, units, 5xx rate, backup age/off-site, disk, memory, TLS); alerts on state change with a next step; 5 tests. Read-only run on this host: all green except the backup check. That failure is expected: it read the deliberately failed test backup. Needs a webhook URL or mailbox; no sendmail on the host |
| Legacy service key exposure (P0-9) | Containment READY; code PASS | Prod `ENCRYPTION_MASTER_KEY` readable by any local user via `systemctl show luxit.service` (verified as `nobody`; same fingerprint as prod). Retirement package `/root/luxit-ops-backups/20261031-legacy-luxit-retirement/`. Vault: MultiFernet rotation, production refuses a bad or missing key (b813224); `scripts/rotate_encryption_key.py` was run on an isolated copy. Runbook: `docs/ops/ENCRYPTION_KEY_ROTATION.md` |

## C. Stripe billing (test mode)

| Item | State | Evidence |
|---|---|---|
| Checkout session creation | PASS (SDK faked) | `test_stripe_billing_endpoints` (server-side lookup keys only, setup fee, authz); journey steps 7–8 |
| Webhook signature | PASS | real HMAC: tampered body, wrong secret and a timestamp older than 300 s are each rejected with no DB write (`test_stripe_lifecycle_sandbox`) |
| Idempotency | PASS | replayed event → `duplicate: true`, no re-apply |
| Activation / payment failure / recovery / upgrade / cancel | PASS | lifecycle test, two tenants; tenant B's state is unchanged throughout |
| Grace period keeps entitlements | FIXED | defect: the webhook writes `grace_period`, but `is_free_plan` only honored `past_due` (which nothing writes), so a declined card dropped a self-serve tenant to free limits immediately. A `past_due` subscription update now also starts the grace clock |
| Out-of-order / superseded-subscription events | FIXED | defect: a late `subscription.deleted` for an old subscription suspended a tenant who had resubscribed; a retried `payment_failed` landing after `payment_succeeded` put a paid tenant into grace. Fixes: per-tenant `billing_event_cursor` plus a current-subscription check |
| Real Stripe test-mode run (Stripe CLI / dashboard) | BLOCKED | no `sk_test_`/`whsec_` on any non-prod instance; 5 lookup keys and prices unverified (D-2) |

## D. SaaS onboarding

| Item | State | Evidence |
|---|---|---|
| Signup → verify → org → onboarding → invite → roles → CRM → SMS → automation → billing portal → password change → cancellation | PASS (in-process) | `test_saas_customer_journey` (Stripe SDK and Twilio REST faked; both webhooks signed for real) |
| Same journey on staging with disposable accounts | OPEN | needs staging deploy approval and a non-prod email sink. `SELF_SERVE_SIGNUP_ENABLED` stays off |

## E. Communications

| Item | State | Evidence |
|---|---|---|
| Tenant Twilio model | BLOCKED (D-6) | BYO-Twilio path exercised in the journey; no connect/number/webhook UI (T-32) until the model is chosen |
| Consent / suppression | PASS | `test_sms_compliance_keywords`, `test_sms_consent_purpose`, `test_segment_management_suppression` (baseline-only failures) |
| Native push credentials | BLOCKED | needs `LUXIT_APNS_KEY_FILE`, `LUXIT_APNS_KEY_ID`, `LUXIT_APNS_TEAM_ID`, `LUXIT_APNS_TOPIC`, `LUXIT_FCM_SERVICE_ACCOUNT_FILE` (0 configured anywhere) |
| Physical-device calling | BLOCKED | handset + LUX Connect staging build; staging uses **prod Twilio credentials** (isolate before any device test) |

## F. Staging release

Package `/root/luxit-ops-backups/20261031-staging-release-candidate/`
(`deploy.sh <full-sha>`, `verify.sh`, `rollback.sh`). Target SHA: the docs commit on top of b9752aa (code identical to b9752aa; `git -C /root/luxit-release-20261029 rev-parse HEAD`). Not run.
Migration rehearsal on an isolated copy of staging: run 1 applied 8 (ledger 42→50), run 2 applied 0, no errors.
Steps: guards (clean tree, staging on 9311cec, memory) → luxstaging dump →
8 ledgered migrations as `luxstaging` → exported tree + `zzz-release-rc-*`
drop-in → restart staging only → verify (unauthenticated probes, ledger,
`billing_event_cursor`, restore drill of the pre-deploy dump). Prod, nginx,
Twilio and env files are untouched.

## External dependencies (owner)

1. Approve the staging deploy of the release HEAD (`deploy.sh $(git rev-parse HEAD)`).
2. D-10: off-site bucket + credentials; alert channel; backup gpg keypair generated off-host.
3. Approve legacy `luxit.service` retirement, then the key rotation (restarts prod web and worker).
4. Stripe test keys (`sk_test_`, `whsec_`) for staging; D-1…D-4 pricing/trial/grace/refund.
5. D-6 tenant messaging model; separate staging Twilio credentials.
6. APNs/FCM credentials; handset session with the LUX Connect project.
