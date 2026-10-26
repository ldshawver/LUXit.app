# Security incident evidence — account takeover / privilege escalation / cross-tenant access

- Collected: 2026-10-19, read-only, production `luxdb` (PG :5432, system id 7599349366402728555), prod code `645dd65`.
- Scope: whether the P0 flaws fixed in `212ae1d` were exploited.
- **Conclusion: no evidence of exploitation was found in any available source. That is not proof of absence.** The evidence windows below are shorter than the exposure windows, and read-only access through the IDOR routes leaves no database trace.

## Exposure windows (when the flaw first shipped, from git history)

| Flaw | Introduced | Evidence coverage |
|---|---|---|
| Reset link shown to requester (`/auth/forgot-password`) | `6bb888f`, 2026-02-27 | app log from 2026-07-09; DB token sequence covers the DB's whole life |
| Legacy `/user/add-user`, `/user/edit-user` without authz | `065a5ac`, 2025-08-23 | app log from 2026-07-09; account fingerprints from 2026-06-19 |
| Admin via self-edited profile `tags`/`segment` | `cef1e30`, 2026-01-24 | same |
| `/saas/*` open to any user; IDOR routes | pre-July 2026 (not bisected) | app log from 2026-07-09 |

## Sources

1. **Structured app log** `/storage/logs/app.log*`: written by the app's own after-request hook with `path`, `statusCode`, `userId`, `companyId`. 119,426 `environment=production` entries, 2026-07-09 07:00 → 2026-10-19 17:23. (Test runs also write `environment=testing` entries into this directory; they were excluded. See "Side findings".)
2. **nginx access logs**: 14 days only (2026-10-05 →).
3. **Database state** compared with dumps from 2026-06-19, 2026-07-18, 2026-08-25 and 2026-09-08.

## Findings

### Password reset
- `password_reset_token`: **0 rows**, and its id sequence is `last_value=1, is_called=false`. **No reset token has ever been inserted in this database.** (Would only be wrong if the sequence was reset or the table recreated.)
- App log: 2 `POST /auth/forgot-password` (2026-10-12 03:45:50 and 03:45:59, anonymous) and 3 `POST /auth/forgot-username` (2026-07-30 08:10:13, 08:10:37; 2026-10-12 20:40:30). **0 requests to `/auth/reset-password/<token>`.**
- nginx (covers only the 10-12 events): the 03:45 requests came from one IP after a failed login. That session made 22 requests in total and went back to the login page. The forgot-*username* requests show the account's username to whoever submits the matching email, so up to 3 username disclosures may have happened. Whether the submitted emails matched an account is not logged.

### Administrator privileges and account integrity
- Only user 1 has `is_admin = true`. That is identical in all four dumps (2026-06-19 → 2026-09-08) and today.
- Password-hash and email fingerprints (md5 of stored values) of every account present in each dump are **unchanged** from 2026-06-19 (users 1, 2, 4), 2026-07-18 (user 5) and 2026-09-08 (users 6, 7, 8) to today.
- No user has ever had `segment`/`tags` containing `admin`.
- `user_company_access` roles and `manage_users_enabled` are identical between the 2026-09-08 dump and today. Only user 1 is an owner or holds manage-users.
- App log: **0 requests** to the legacy `/user/add-user` or `/user/edit-user`, and **0** to `PUT /api/user/profile` or `/user/profile/edit`. All 34 user-management requests (`/user/add`, `/user/edit`, `/user/delete`, `/api/user/<id>/access`) were made by user 1.

### `/saas` console and subscription state
- App log: `/saas/` opened twice, both by user 1. The other 13 hits are anonymous bot probes (`/saas/.env` → 404, `/saas` → 308 to login).
- `saas_automation_log`: no `manual` account/license/onboarding/deal events at all. The only rows are 3 Stripe `signature_invalid` (July).
- Company billing fields are still defaults/NULL. `saas_license` has 0 rows.

### Cross-tenant object access (IDOR routes)
- App log: **0 requests** to any vulnerable object route (`/campaigns/<id>…`, `/automations/<id>/…`, `/contacts/<id>/activities…`, `/sms/<id>/analytics`, `/api/calendar/events/<type>_<id>`, `/crm/deals/<id>`, `/blog/<id>/edit`, `/seo/audit/<id>`, `/email-builder/<id>`).
- The target tables are empty in production: campaign, automation, deal, calendar_event, blog_post, seo_audit, social_post = **0 rows**. So for those routes there was nothing to read or modify.
- `contact_activity` rows whose tenant differs from their contact's tenant: **0**.
- SMS campaigns: all belong to tenant 1 (one row has NULL company); none was modified after 2026-10-06.
- Structural note: every non-admin user belongs only to tenant 1, and tenant 2 (Adiken Inc) has only user 1. Cross-tenant reads would have meant tenant-1 staff reading tenant-2 contacts; none of the contact routes involved shows any access.

## Limits (why this is not proof)
- No request-level evidence before 2026-07-09 (exposure for the reset flaw began 2026-02-27; for the legacy user routes 2025-08-23).
- The app log is written by the application itself. It would miss requests that bypassed Flask's after-request hook, and it records no IP addresses.
- Read-only access (viewing a page) changes no rows, so database comparison only detects *writes*.
- No database audit trigger exists for `user`, `user_company_access` or `company`.
- Users 7 and 8 have empty password hashes and are inactive (smoke-test accounts from 2026-09-02). They cannot log in, but should be confirmed as intended.
- User id 3 does not exist in any dump back to 2026-06-19. Its history is unknown.

## Side findings
- **Test runs write into the production log directory.** `/storage/logs` is the first writable candidate in `diagnostics_service.log_dir()`, and the test suite does not override it. Every session's pytest runs (including this audit's before 18:04 UTC, and a concurrent peer session) appended `environment=testing` entries: 5,700+ today alone. No rotation was triggered and nothing was deleted (BACKUPS=10, 6 files exist). Mitigation for test runs: set `DIAGNOSTICS_LOG_DIR` to a scratch directory. Fix: make tests default to a temp dir (P2).
- `serveradmin` (GitHub Actions deploy user) can read the production `ENCRYPTION_MASTER_KEY` (see LAUNCH_READINESS §6).

## Reproduce
- Log scan: `python3 deploy/ops/security_log_scan.py out.json` (regexes listed in the script).
- DB checks are plain `SELECT`s listed above; dump comparisons use `pg_restore --data-only -t user -f -`.
