---
name: PWA access control — PostHog removed
description: /app/inbox access is controlled by UserCompanyAccess flags, not PostHog feature flags.
---

## Rule
Never use PostHog feature flags to gate access to /app/inbox (the Mobile Inbox PWA). Access is controlled per-user via `UserCompanyAccess`.

## New columns (added via migrate_db.py)
- `user_company_access.can_access_mobile_inbox` BOOLEAN DEFAULT 0
- `user_company_access.can_access_full_app` BOOLEAN DEFAULT 1

## Helper methods on UserCompanyAccess
- `has_mobile_inbox_access()` → True for ROLE_OWNER, ROLE_ADMIN, ROLE_INBOX_ONLY, or can_access_mobile_inbox=True
- `has_full_app_access()` → False only for ROLE_INBOX_ONLY or can_access_full_app=False

## New roles added
manager, staff, inbox_only (in addition to owner, admin, editor, viewer)

## Access check in inbox_pwa.py
`_check_mobile_inbox_access(user, company)` — user.is_admin bypasses; otherwise queries UserCompanyAccess.

## _get_company() fix
No longer falls back to `Company.query.first()`. Returns None for unlinked users (non-admins). Platform admins get first active company as fallback.

## Login redirect
`_hub_redirect()` in auth.py checks: if user has inbox_only role (no full_app_access, has mobile_inbox_access) → redirect /app/inbox instead of dashboard.

## API endpoint
`POST /api/user/<id>/access` — accepts {role, can_access_mobile_inbox, can_access_full_app}, protected by admin/company-admin check.

## Error pages
- `templates/no_company.html` — shown when user has no company linked
- `templates/inbox_access_denied.html` — shown when user lacks mobile inbox access

## VPS deploy note
Run `python3 scripts/migrate_db.py` on VPS after deploy to add the two new columns.

**Why:** PostHog was configured with SMS-features flag explicitly set to False, blocking all PWA access. The PWA must be accessible without any PostHog dependency so staff phones can log in and message customers without cloud feature-flag infrastructure.
