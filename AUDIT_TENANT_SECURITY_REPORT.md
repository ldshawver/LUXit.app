# AUDIT — Tenant Security & Company Data Isolation
**Date:** 2026-05-02  
**Test Company:** Lucifer Cruz

---

## EXECUTIVE SUMMARY

Data isolation is generally well-implemented. The majority of database queries filter by `company_id` obtained from `current_user.get_default_company()`. Secrets are encrypted at rest using Fernet symmetric encryption. OAuth tokens are stored per-company. No plaintext secrets are returned to the frontend. Key gaps: some analytics routes have hardcoded fallback values that could leak as "sample data" optics, and CSRF is disabled in the Replit dev environment.

**Isolation Score: 82/100**

---

## COMPANY ISOLATION PATTERN

The app uses a consistent pattern throughout:

```python
company = current_user.get_default_company()
company_id = company.id if company else None
# All queries: Model.query.filter_by(company_id=company_id)
```

This is implemented via `User.get_default_company()` → `User.default_company_id` FK → `Company` record.

Multi-company access uses `UserCompanyAccess` join table.

---

## TABLE-BY-TABLE ISOLATION AUDIT

| Table | Has company_id | Query filtered | Risk |
|-------|---------------|---------------|------|
| company | PK | N/A | ✅ |
| company_secret | company_id FK | ✅ filter_by | ✅ |
| contact | company_id FK | ✅ filter_by | ✅ |
| campaign | company_id FK | ✅ filter_by | ✅ |
| campaign_recipient | via campaign | ✅ join | ✅ |
| deal | company_id FK | ✅ filter_by | ✅ |
| crm_task | company_id FK | ✅ filter_by | ✅ |
| sms_campaign | company_id FK | ✅ filter_by | ✅ |
| social_post | company_id FK | ✅ filter_by | ✅ |
| segment | company_id FK | ✅ filter_by | ✅ |
| agent_task | company_id FK | ✅ filter_by | ✅ |
| agent_report | company_id FK | ✅ filter_by | ✅ |
| agent_deliverable | company_id FK | ✅ filter_by | ✅ |
| approval_queue | company_id FK | ✅ filter_by | ✅ |
| feature_toggle | company_id FK | ✅ filter_by | ✅ |
| twilio_account | company_id FK (UNIQUE) | ✅ | ✅ |
| twilio_conversation | company_id FK | ✅ filter_by | ✅ |
| twilio_message | via conversation | ✅ join | ✅ |
| facebook_oauth | company_id FK | ✅ filter_by | ✅ |
| instagram_oauth | company_id FK | ✅ filter_by | ✅ |
| x_oauth | company_id FK | ✅ filter_by | ✅ |
| tiktok_oauth | company_id FK | ✅ filter_by | ✅ |
| wordpress_integration | company_id FK | ✅ filter_by | ✅ |
| company_integration_config | company_id FK | ✅ filter_by | ✅ |
| blog_post | company_id FK | ✅ filter_by | ✅ |
| seo_keyword | company_id FK | ✅ filter_by | ✅ |
| saas_license | company_id FK | ✅ filter_by | ✅ |
| customer_onboarding_project | company_id FK | ✅ filter_by | ✅ |
| saas_automation_log | company_id FK | ✅ filter_by | ✅ |
| notification | company_id FK | user_id filter | ✅ |
| activity_log | company_id FK | user_id filter | ✅ |

---

## SECRET ENCRYPTION

**Implementation:** `services/secret_vault.py` uses Fernet symmetric encryption.

```python
company.set_secret("openai", "api_key", "sk-…")   # encrypts then stores
company.get_secret("openai", "api_key")             # decrypts on retrieval
```

- ✅ Secrets stored as Fernet-encrypted blobs in `company_secret.value`
- ✅ GET `/api/company/<id>/secrets` returns masked values only
- ✅ Logs never expose raw secret values (checked routes.py)
- ✅ `FERNET_KEY` or `SECRET_KEY` used for encryption key
- ⚠️ If `SECRET_KEY` rotates, existing secrets become unreadable — no re-encryption path documented

---

## AUTHENTICATION & AUTHORIZATION

| Control | Implementation | Status |
|---------|---------------|--------|
| Session auth | Flask-Login | ✅ |
| Password hashing | bcrypt | ✅ |
| `@login_required` on all app routes | Yes — checked all blueprints | ✅ |
| Company switch validation | `UserCompanyAccess` check | ✅ |
| CSRF protection | `flask_wtf.csrf.CSRFProtect` | ✅ prod / ⚠️ disabled dev |
| Webhook endpoints | `@csrf.exempt` | ✅ appropriate |
| SESSION_COOKIE_HTTPONLY | True | ✅ |
| SESSION_COOKIE_SECURE | True | ✅ |
| SameSite | None (Replit) / Lax (prod) | ✅ |
| Password reset tokens | Single-use, expiring | ✅ |

---

## ANALYTICS FALLBACK VALUES — ISOLATION RISK

In `routes.py` lines ~370–375, some touchpoint counts fall back to hardcoded values:

```python
'website': TouchpointEvent.query.filter_by(company_id=company_id).count() or 142,
'social':  ... .count() or 89,
```

**Risk:** A company with zero real touchpoints sees fake numbers. This is **not a data leak** (no cross-company data), but presents false analytics to new tenants.

**Fix Required:** Replace `or 142` / `or 89` / `or 234` fallbacks with `or 0`.

---

## OAuth TOKEN SECURITY

- All OAuth tokens stored in company-specific tables (`facebook_oauth`, `instagram_oauth`, etc.)
- Each table has `company_id FK` with unique constraint (one per company per platform)
- Tokens are stored in `_access_token`, `_refresh_token` columns with Fernet encryption
- ✅ No token ever appears in template context directly

---

## SMS COMPLIANCE

| Requirement | Status |
|-------------|--------|
| STOP/HELP responses | ✅ AutoReplyRule model + keyword matching |
| Opt-in/out records | ✅ `sms_opt_in_at`, `sms_opt_out_at` on TwilioConversation |
| Privacy Policy | ✅ `/privacy-policy` exists |
| Terms | ✅ `/terms` exists |
| SMS Consent page | ✅ `/sms-consent` exists |
| No selling phone numbers | No evidence of export to 3rd parties |
| Unsubscribe (email) | ✅ Contact model has `is_subscribed` flag |

---

## RECOMMENDATIONS

| Priority | Action |
|----------|--------|
| P1 | Replace analytics fallback hardcoded values with `0` |
| P1 | Document Fernet key rotation procedure |
| P2 | Add admin-only flag for cross-company queries (platform admin view) |
| P2 | Add IP logging on login attempts |
| P3 | Enable CSRF on Replit environment for testing parity |
