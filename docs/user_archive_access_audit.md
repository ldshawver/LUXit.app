# User archive access audit

Standard user deletion is now archival. Historical records keep their user foreign keys; access-bearing records are deactivated or ignored for inactive users.

| Access-bearing path | Archive action | Runtime guard |
|---|---|---|
| Password login | `User.active` is set false; login rejects inactive users. | `auth.login` checks `user.active`. |
| Existing Flask session / remember cookie | `User.session_revoked_at` is updated; old session markers no longer match. | `app.before_request` logs out inactive/revoked sessions. |
| Company membership via `default_company_id` | Archive clears `default_company_id` when archiving from that company. | `User.get_default_company` cannot resolve inactive archived users because loader denies them. |
| `UserCompanyAccess` | Tenant row is set `is_active=false`, previous role is preserved. | `get_company_access` and comms permission helpers require active access. |
| Manage Users | Access row toggles are blocked for archived users. | `/api/user/<id>/access` returns 409 for inactive users. |
| PWA/mobile inbox | PWA and mobile flags are disabled on the tenant access row. | `user_access_for_company` rejects inactive users/access rows. |
| Phone-number permissions | Per-number booleans are set false for the archived tenant. | Phone-number filters require explicit true permissions. |
| Push/device access | Active push subscriptions for the tenant are disabled. | Push APIs filter `PushSubscription.is_active=true`. |
| Provider/API credentials | Historical credential rows are preserved; user sessions are invalidated. | User-bound operational routes must still pass active-session checks. |
| Audit/history rows | Preserved. | Archive/restore write `IntegrationAuditLog` events. |
