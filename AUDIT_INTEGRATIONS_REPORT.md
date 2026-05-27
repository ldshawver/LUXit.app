# AUDIT — Integrations & API Connections
**Date:** 2026-05-02  
**Test Company:** Lucifer Cruz

---

## SUMMARY

| Integration | Config UI | Encrypted Storage | SDK Used | Live Test | Status |
|------------|-----------|------------------|---------|-----------|--------|
| OpenAI | ✅ `/settings/integrations` | ✅ Fernet | ✅ openai lib | ⚠️ Key invalid in dev | ⚠️ Config OK, key needed |
| Twilio | ✅ `/twilio/settings` | ✅ Fernet | ✅ twilio lib | ✅ | ✅ Working |
| WordPress/WooCommerce | ✅ `/settings/integrations` | ✅ Fernet | ✅ woocommerce lib | ✅ | ✅ Working |
| X/Twitter | ✅ `/x-integration` | ✅ XOAuth model | ✅ tweepy | ✅ OAuth | ✅ Working |
| Facebook | ✅ social OAuth | ✅ FacebookOAuth | ✅ requests | ✅ OAuth | ✅ Working |
| Instagram | ✅ social OAuth | ✅ InstagramOAuth | ✅ requests | ✅ OAuth | ✅ Working |
| TikTok | ✅ social OAuth | ✅ TikTokOAuth | ✅ requests | ✅ OAuth | ✅ Working |
| Stripe | ❌ Not in integrations UI | ⚠️ Env var only | ❌ No SDK routes | ❌ | ❌ Missing |
| Supabase | ❌ Not in integrations UI | ⚠️ Company.supabase_tenant_id | ❌ No SDK | ❌ | ❌ Missing |
| n8n | ❌ Not in integrations UI | ⚠️ Company secret only | ✅ HTTP POST | ⚠️ In saas_mgmt only | ⚠️ Partial |
| MyPayLink | ❌ Not in integrations UI | ⚠️ Company.mypaylink_id | ❌ No API calls | ❌ | ❌ Missing |
| Google Analytics/GA4 | ✅ Config stored | ✅ Fernet | ❌ GA4 API not called | ❌ | ⚠️ Config only |
| Google Ads | ✅ Config stored | ✅ Fernet | ❌ | ❌ | ⚠️ Config only |
| MS365 | ✅ Config stored | ✅ Fernet | ✅ SMTP | ⚠️ Needs valid creds | ⚠️ Partial |

---

## DETAILED INTEGRATION STATUS

### OpenAI
- **Config path:** `CompanyIntegrationConfig` + `CompanySecret(key='api_key')`
- **UI:** `/settings/integrations` → OpenAI card
- **Usage:** `ai_agent.py`, `ai_action_executor.py`, all 11 AI agents
- **Issue:** Dev environment has expired/invalid key (401 in logs)
- **Fix:** Set valid `OPENAI_API_KEY` in VPS `.env` or company secrets for Lucifer Cruz

### Twilio
- **Config path:** `TwilioAccount` model (per-company, unique)
- **UI:** `/twilio/settings`
- **Features working:** SMS inbound/outbound, voice calls, inbox, auto-reply rules, business hours, call logs, analytics
- **Known issue:** Forward number `+12792860000` getting error 30034 (carrier block) — separate from app logic
- **Status:** ✅ Fully integrated

### WordPress/WooCommerce
- **Config path:** `WordPressIntegration` model
- **UI:** `/settings/integrations`
- **Features:** Product sync, order sync, contact import from subscribers
- **Status:** ✅ Working

### Social Media (Facebook, Instagram, TikTok, X)
- **Config path:** Per-platform OAuth models
- **UI:** `/social-media`, `/facebook-accounts`, `/social/connect-account`
- **Features:** OAuth flow, post scheduling, engagement metrics where APIs allow
- **Status:** ✅ Working (dependent on platform API access levels)

### Stripe — MISSING INTEGRATION
- **Current state:** `STRIPE_SECRET_KEY` env var checked in system status; `stripe_lib.Webhook.construct_event` in saas_mgmt.py
- **Missing:**
  - No Stripe customer creation on company add
  - No subscription lookup/display
  - No invoice list
  - No payment method management
  - Stripe not in integrations UI
- **Required fix:** Add `stripe` to requirements, add billing routes, add to integrations registry

### Supabase — MISSING INTEGRATION
- **Current state:** `Company.supabase_tenant_id` field only
- **Missing:**
  - No `supabase-py` SDK calls
  - No tenant creation
  - No user sync
  - No data queries
- **Required fix:** Install `supabase`, add `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` env vars, add tenant provisioning

### n8n — PARTIAL INTEGRATION
- **Current state:** `_fire_n8n()` in `saas_mgmt.py` fires on SaaS lifecycle events
- **Missing:**
  - Not in integrations registry UI
  - Cannot save n8n webhook URL through UI (must use company secrets API)
  - Not triggered from CRM events (new contact, new deal, campaign sent)
- **Fix:** Add n8n to integrations registry, expose webhook URL field in UI, add hooks in key routes

### MyPayLink — MISSING INTEGRATION
- **Current state:** `Company.mypaylink_id` field only
- **Missing:**
  - No API client
  - No payment/payout routes
  - Not in integrations UI
- **Required fix:** Add MyPayLink API client, add to integrations registry

### Google Analytics/GA4 — CONFIG ONLY
- **Current state:** Config stored in `CompanyIntegrationConfig`
- **Missing:** No GA4 Reporting API calls; analytics rely on internal data only
- **Fix:** Add `google-analytics-data` lib, implement GA4 sessions/events pull

---

## INTEGRATIONS REGISTRY — MISSING SERVICES

The following should be added to `advanced_config.py` service registry:

```python
# Add to MIGRATIONS / service definitions:
("stripe",    "Stripe",    ["publishable_key", "secret_key", "webhook_secret"])
("supabase",  "Supabase",  ["url", "anon_key", "service_role_key"])
("n8n",       "n8n",       ["webhook_url", "api_key"])
("mypaylink", "MyPayLink", ["api_url", "api_key", "account_id"])
```

---

## RECOMMENDATIONS

| Priority | Action | File |
|----------|--------|------|
| P0 | Set valid OpenAI key on VPS | `.env` / company secrets |
| P1 | Add Stripe SDK + billing routes | `saas_billing.py` (new) |
| P1 | Add Supabase SDK + tenant provisioning | `saas_provisioning.py` (new) |
| P1 | Add Stripe/Supabase/n8n/MyPayLink to integrations registry | `advanced_config.py` |
| P2 | Add GA4 Reporting API calls | `routes.py` analytics section |
| P2 | Expose n8n webhook URL in integrations UI | `advanced_config.py` |
| P3 | Add MyPayLink API client | `mypaylink_service.py` (new) |
