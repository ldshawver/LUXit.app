# SECURITY & COMPLIANCE AUDIT
**Date:** 2026-05-02

---

## AUTHENTICATION

| Control | Implementation | Status |
|---------|---------------|--------|
| Login | Flask-Login + bcrypt | ✅ |
| Password hashing | `bcrypt.generate_password_hash` | ✅ |
| Password reset | Single-use expiring tokens (`PasswordResetToken`) | ✅ |
| Session management | Flask sessions, `SESSION_COOKIE_HTTPONLY=True` | ✅ |
| Session security | `SESSION_COOKIE_SECURE=True` | ✅ |
| SameSite | `None` (Replit proxy) / `Lax` (VPS) | ✅ |
| Login required | `@login_required` on all app routes | ✅ |
| Auth debug endpoint | `GET /auth/debug-session` | ⚠️ Should be disabled in production |

## AUTHORIZATION

| Control | Status | Notes |
|---------|--------|-------|
| Company-scoped queries | ✅ | `get_default_company()` pattern |
| Company access control | ✅ | `UserCompanyAccess` table |
| Company switch validation | ✅ | Verifies user has access before switching |
| Admin routes protection | ✅ | `@login_required` |
| Webhook CSRF exemption | ✅ | `@csrf.exempt` on Twilio/Stripe endpoints |
| No unauthenticated data access | ✅ | All `/api/*` routes require login |

## CSRF

| Control | Status | Notes |
|---------|--------|-------|
| CSRFProtect installed | ✅ | `flask_wtf.csrf.CSRFProtect` |
| CSRF on all forms | ✅ prod | Production VPS |
| CSRF in Replit dev | ⚠️ Disabled | `WTF_CSRF_ENABLED=not is_replit` |
| Webhook endpoints exempt | ✅ | `@csrf.exempt` appropriate |
| CSRF error handler | ✅ | Returns JSON error |

**Recommendation:** Consider enabling CSRF in Replit for test parity with production.

## SECRETS & ENCRYPTION

| Control | Status |
|---------|--------|
| API keys encrypted at rest | ✅ Fernet symmetric encryption |
| OAuth tokens encrypted | ✅ Per-platform OAuth models with Fernet |
| No secrets in templates | ✅ Verified — masked values only in context |
| No secrets in logs | ✅ Verified — no plaintext secrets in log calls |
| `.env` gitignored | Must verify `.gitignore` |
| Database file gitignored | Must verify `.gitignore` |
| `FERNET_KEY` rotation procedure | ❌ Not documented |

## WEBHOOK SECURITY

| Webhook | Signature Verification | Status |
|---------|----------------------|--------|
| Twilio SMS inbound | Twilio signature header | Should be implemented |
| Twilio voice | Twilio signature header | Should be implemented |
| Stripe webhook | ✅ `stripe.Webhook.construct_event` | ✅ When `STRIPE_WEBHOOK_SECRET` set |
| Zapier contact webhook | ❌ No signature check | ⚠️ Auth by secret URL only |

**Fix:** Add Twilio request signature validation to `twilio_sms.py` inbound webhook handlers.

## FILE UPLOADS

| Control | Status |
|---------|--------|
| File type validation | Should be reviewed |
| Max file size | Should be reviewed |
| Upload to static/uploads | Stored locally |
| Path traversal protection | Flask's `secure_filename` should be used |

## CORS

| Control | Status |
|---------|--------|
| CORS headers | Not explicitly set (Flask default — same-origin) |
| API endpoints | Behind `@login_required` — session-based, not CORS-dependent |

## HTTPS / PROXY

| Control | Status |
|---------|--------|
| HTTPS on VPS | nginx terminates TLS | ✅ |
| Proxy headers | `ProxyFix` in wsgi.py | Should verify |
| HSTS | Should be set in nginx |

## LOGGING & AUDIT

| Control | Status |
|---------|--------|
| Activity log | ✅ `ActivityLog` model per action |
| Approval audit log | ✅ `ApprovalAuditLog` model |
| Integration audit log | ✅ `IntegrationAuditLog` model |
| SaaS automation log | ✅ `SaasAutomationLog` |
| Error logging | ✅ Error logger service |
| Login/logout events | ⚠️ Not explicitly logged to ActivityLog |

## SMS / EMAIL COMPLIANCE

| Requirement | Status |
|-------------|--------|
| STOP keyword handling | ✅ AutoReplyRule with keyword matching |
| HELP keyword | ✅ AutoReplyRule |
| Opt-in records | ✅ `sms_opt_in_at` on TwilioConversation |
| Opt-out records | ✅ `sms_opt_out_at` on TwilioConversation |
| Privacy policy | ✅ `/privacy-policy` |
| Terms of service | ✅ `/terms` |
| SMS consent page | ✅ `/sms-consent` |
| Email unsubscribe | ✅ `Contact.is_subscribed` flag |
| Email unsubscribe link in campaigns | Must verify campaign templates |
| GDPR data deletion | ✅ `/data-deletion` request form |

---

## PRIORITY FIXES

| Priority | Issue | Action |
|----------|-------|--------|
| P1 | `GET /auth/debug-session` exposed in production | Disable or add IP restriction |
| P1 | No Twilio webhook signature validation | Add `twilio.request_validator` check |
| P1 | Zapier webhook has no signature | Add HMAC secret to header check |
| P1 | `FERNET_KEY` rotation not documented | Document rotation procedure |
| P1 | Verify `.gitignore` excludes `.env` and SQLite DB | Check and update `.gitignore` |
| P2 | Login events not logged to ActivityLog | Add on successful/failed login |
| P2 | File upload security review | Audit all `request.files` handling |
| P3 | HSTS header in nginx config | Add `Strict-Transport-Security` header |
