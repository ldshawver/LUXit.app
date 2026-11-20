# Staging deployment plan — P0 security release

Status: **PLAN ONLY. Not executed.** Staging and production deploys each need separate authorization.

## 1. What is being released

| | |
|---|---|
| Release commit (required) | `212ae1d45c27dae5dfbab1f9725150920cb6cd54` (tree `5aed0ff7`): *Security: close account takeover, privilege escalation and cross-tenant access* |
| Optional add-on | `49b40ff984011435130260d33d034b1beac3aa5f` (tree `1965f38b`): auth rate limits + stored-XSS mitigation. Decide before staging; the default plan below is **212ae1d alone**. |
| Parent | `645dd6530fdc4789823e2d0753cc98bcf40f8ad8` = current production |
| Branch | `saas/gtm-readiness-20261019` (local only; not pushed, not merged) |
| Excluded | everything after `49b40ff` on the branch (unsubscribe, email-send honesty, DealActivity + migration, subscriber sync, ops scripts), the PWA phone overhaul (`32814a7`), LUX Connect native |

Files changed `645dd65..212ae1d` (14): `app.py` (+6: rollback before error logging), `auth.py`, `models.py` (is_admin_user only), `routes.py`, `saas_mgmt.py`, `user_management.py`, `services/tenant_scope.py` (new), `templates/auth/{forgot_password,forgot_username}.html`, `templates/auth/{email_password_reset,email_forgot_username}.html` (new), `templates/base.html`, `templates/edit_user.html`, `tests/test_gtm_p0_security.py` (new).

Add-on `49b40ff` files: `app.py`, `auth.py`, `extensions.py`, `templates/{landing_page_builder,newsletter_archive_public,preview_email,preview_template,view_agent_report}.html`, 2 test files.

**Migrations: none.** No schema change, no data change, no new dependency (Flask-Limiter 4.0.0 used by the add-on is already installed and pinned). No config/env change.

## 2. Behavior changes operators will notice
1. **Password reset now requires email delivery.** The reset link is only emailed (Microsoft Graph, SMTP fallback). If email delivery is broken, users can no longer self-reset. Today they "could" only because the link was shown on screen. Fallback: platform admin assists (contact shown on the page: luke@adiken.com).
2. Forgot username is emailed, never shown.
3. Tenant admins no longer see the "Platform / SaaS Command Center" sidebar section. `/saas/*` returns 403 to them.
4. `/user/edit/<id>` works again (it crashed with 500 on every save). Role dropdown shows the real workspace role.
5. Tenant admins cannot edit platform admins or users who also belong to another workspace. Only owners can grant/alter the owner role. Nobody (except platform admins) can change their own access grants.
6. Platform admins see objects only in their *current* workspace (switch workspace to reach another tenant's campaign, contact, etc.).
7. With `49b40ff`: more than 10 login POSTs per minute (50/hour) from one IP per worker get HTTP 429.

## 3. Staging approach

Current staging (`/srv/lux-email-bot-staging` @ `c2eda12`, :8004, `staging.luxit.app`) runs the phone/LUX Connect native line that another workstream is testing. Replacing it would disrupt that work and would also test a different lineage than production. Recommended: **a parallel staging instance** of the exact release commit:

- Code: `/srv/lux-email-bot-staging-sec-212ae1d` (git archive of 212ae1d; owner `luxstaging`; ProtectHome forbids /root).
- Unit: `lux-email-bot-staging-sec.service`, copy of the staging unit with `WorkingDirectory` → the new dir, `--bind 127.0.0.1:8006`, `ReadWritePaths` → new `instance/` + `logs/`, same `EnvironmentFile`s (staging DB `luxstaging` :5434, `LUXIT_TWILIO_MODE=disabled`), plus `Environment=DISABLE_AGENT_SCHEDULER=true` and `Environment=DIAGNOSTICS_LOG_DIR=/srv/lux-email-bot-staging-sec-212ae1d/logs`.
- Venv: reuse `/srv/lux-email-bot-staging/.venv` (read-only use; the commit adds no dependencies).
- **No nginx/routing change.** Acceptance runs against `http://127.0.0.1:8006` from the host.
- Staging DB compatibility: the staging ledger holds all 41 production migrations plus `20261009_lux_connect_native.sql` (an extra table that 645dd65-based code ignores).

## 4. Backup before staging
1. `pg_dump -Fc` of `luxstaging` → `/root/luxit-ops-backups/<stamp>-staging-sec/luxstaging.dump`; verify with `pg_restore -l`.
2. Record staging fingerprints: `user` (id, is_admin, md5(password_hash), md5(email)), `user_company_access`, `company` billing columns.

## 5. Acceptance tests (all must pass, with output saved as evidence)

**A. Automated (host, network-isolated, logs redirected)**

    cd /srv/lux-email-bot-staging-sec-212ae1d
    unshare -n env -i HOME=/tmp PATH=/usr/bin:/bin LANG=C.UTF-8 DIAGNOSTICS_LOG_DIR=/tmp/sec-acc-logs \
      /srv/lux-email-bot-staging/.venv/bin/python -m pytest tests -q -p no:cacheprovider

Pass criteria: `tests/test_gtm_p0_security.py` 33/33. Full suite: run the same command on a `git archive 645dd65` export with the same venv first; the 212ae1d failure/error set must equal that baseline (0 new failures). Reference on the prod venv 2026-10-19: 101 F / 18 E on both.

**B. Live HTTP against :8006 with synthetic staging accounts** (create `sec-acc-owner-a`, `sec-acc-viewer-a`, `sec-acc-owner-b` in two `ZZ-SEC-ACC` tenants; never real people):
1. `POST /auth/forgot-password` for an existing and a nonexistent identifier → identical page, no `/auth/reset-password/` in the body. With mail capture (see note), the email holds the link; that link works once.
2. `POST /auth/forgot-username` → username absent from the body.
3. As viewer: `POST /user/add-user` with `is_admin=on` → no user created. `POST /user/edit-user/<owner-b>` → unchanged. `PUT /api/user/profile {"tags":["admin"]}` then `GET /saas/` → 403.
4. As owner-a: `GET /saas/` → 403. `POST /saas/accounts/<a>/edit subscription_tier=enterprise` → 403, unchanged.
5. As owner-a against tenant-b objects (create one campaign, SMS campaign, contact in tenant b): `/campaigns/<id>` (no data), `/campaigns/<id>/send` (status unchanged), `/sms/<id>/analytics` 404, `PATCH/GET /api/calendar/events/sms_<id>` 404, `/contacts/<id>/activities` 404.
6. Role escalation: owner-a edits viewer-a role to `god_mode` → refused. A manage-users admin cannot make themselves owner (403).
7. Regression: owner-a can still log in, add a user, edit a user's role, view own campaigns/contacts/inbox, and open the PWA inbox. A platform admin can open `/saas/`.
8. Re-check fingerprints from §4: only the synthetic accounts changed.

Note on email: staging must not deliver mail to real people. Either run B.1 with the app's `EmailService.send_email` pointed at a capture mailbox you approve, or accept the automated test's mocked delivery and verify real delivery separately (see §7 risk).

**C. Clean-up**: delete the synthetic tenants/users; stop and remove the parallel unit and directory; `daemon-reload`.

## 6. Rollback (staging)
`systemctl disable --now lux-email-bot-staging-sec.service`, remove the unit and directory, `daemon-reload`. Existing staging (:8004) is never touched. If any staging data was altered unexpectedly, restore from the §4 dump.

## 7. Production release (separate approval) — proposed procedure, same pattern as 645dd65
1. `pg_dump -Fc luxdb` → `/root/luxit-database-backups/luxit-before-212ae1d-<stamp>.dump` + `pg_restore -l` check.
2. `git worktree add --detach /root/luxit-prod-212ae1d-<date> 212ae1d`; copy `instance/` from `/root/luxit-prod-645dd65-20261006`.
3. Drop-in `/etc/systemd/system/lux-email-bot.service.d/zz-release-212ae1d-<date>.conf` (sorts after the 645dd65 one): `WorkingDirectory` + `GIT_SHA`/`APP_VERSION`. ExecStart unchanged.
4. `systemctl daemon-reload && systemctl restart lux-email-bot`; check `/health` 200, NRestarts 0, master + workers cwd = new dir, no tracebacks.
5. Smoke (read-only + anonymous): `/auth/forgot-password` page renders; `GET /saas/` as anonymous → login redirect; a platform-admin login works.
6. **Rollback:** `rm` the 212ae1d drop-in, `daemon-reload`, `restart` → back to `/root/luxit-prod-645dd65-20261006`. No schema to revert.
7. Worker and ghost service untouched by this release.

Risk to verify before or at prod release: production email delivery (Graph/SMTP) for reset emails. It was not tested in this audit because that would send a real message.
