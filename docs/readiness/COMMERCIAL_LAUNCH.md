# Commercial launch requirements

What the code does today, the owner decision each item needs, and the
recommended default. Code facts are from `release/saas-native-20261029`
(2026-11-06). Decisions D-1 … D-11 are listed in `LAUNCH_READINESS.md`.
Prices from third parties (Twilio, Stripe) are list prices as last known.
**Verify them in each account before publishing.**

## 1. Plans and entitlements (enforced server-side)

| Plan | How a workspace gets it | Seats | Contacts included | AI | Phone / SMS | Source of truth |
|---|---|---|---|---|---|---|
| Free | self-serve signup, no subscription | 1 | 250 (hard cap, 402 on create) | no (402) | no | `services/entitlements.py` |
| Starter | Stripe `luxit_starter_monthly` (site says $199/mo) | 5, +`luxit_additional_account_monthly` per extra seat | 2,500, then metered overage | yes | only with a phone/SMS license (see §3) | `services/stripe_billing.py` |
| Professional | Stripe `luxit_professional_monthly` (site says $499/mo) | unlimited | 50,000, then metered overage | yes | as Starter | same |
| Managed (pre-SaaS tenants) | no `company_plan_policy` row | unrestricted | unrestricted | yes | per license | unchanged legacy behavior |

One-time setup fee: `luxit_setup_fee_once`, added to the first checkout only.
Workspaces created by a member (not a platform admin) start on Free (T-27).

**Contact overage** (fixed 2026-11-06): `contacts_used` was never maintained,
so overage was always 0, and the legacy Stripe usage-record call no longer
exists in the pinned SDK. The new path is a daily meter event (`scripts/report_contact_usage.py`,
`deploy/billing/`). It needs a Billing Meter with `last` aggregation attached
to the `luxit_contacts_usage_monthly` price; the preflight checks this.

**Decision D-2:** confirm prices, setup fee amount, overage unit price.
**Recommended:** keep $199 / $499 as on the site. Overage at a round per-contact
price (e.g. $0.01/contact/month) billed on the meter. Setup fee only for
Professional, or waived for self-serve.

## 2. Trial, payment failure, cancellation

| Event | What the code does |
|---|---|
| Trial | none. The free plan is the trial. `trialing` status is honored as paid if Stripe sends it |
| Card declined | Stripe `past_due`, then 14-day grace (`BILLING_GRACE_DAYS`) with paid entitlements kept (fixed b9752aa), then suspended |
| Phone/SMS license past due | separate 7-day license grace (`tenant_license.grace_period_days`) |
| Cancel | Stripe customer portal. `cancel_at_period_end` keeps access to the period end; `subscription.deleted` sets status canceled and drops to free limits |
| Close account | owner, after cancelling: Billing → Close this workspace (a2f1a48). Everyone loses access; data kept 30 days (`LUXIT_WORKSPACE_RETENTION_DAYS`) then purged by operator script; export available before closing |
| Refunds | none in code; Stripe dashboard only |

**D-1 / D-3 / D-4. Recommended:** no separate trial (free plan + card at
upgrade). Use **one** grace period: set the license grace to 14 days to match
billing, so phone service doesn't stop a week before the rest of the account.
No prorated refunds; cancellation takes effect at period end; first-month refund
at support's discretion. Publish this in the Terms and next to the checkout button.

## 3. Messaging model: bring-your-own Twilio vs platform subaccounts (D-6, T-32)

Today: a tenant can only send SMS/voice if it has its own `TwilioAccount` row
**and** an active phone/SMS license. New workspaces never get the platform
sender (T-16). The in-app BYO connect flow exists since eb3c53f (owner/admin,
credentials validated against Twilio and vault-encrypted, number ownership
proven through the tenant's credentials, license granted to paying workspaces);
tested against a faked Twilio only. Live check needs separate staging Twilio credentials.

| | A. Bring your own Twilio (BYO) | B. Platform-owned Twilio subaccounts |
|---|---|---|
| Who pays Twilio | tenant, directly | LUXit, then re-bills |
| 10DLC brand + campaign registration | tenant, in their Twilio console | LUXit as ISV for every tenant (secondary customer profiles, campaign vetting fees per tenant) |
| Compliance liability | tenant is the registered sender | LUXit shares it; one bad tenant can hurt the platform's reputation |
| Usage metering / spend caps in LUXit | not needed | required before launch (per-tenant counters, caps, overage price) |
| Build needed | connect form (SID + auth token or API key, validated), number pick from their account, webhook setup, disconnect/rotate, license grant | all of A plus subaccount create/close, number purchase, 10DLC ISV API, usage metering, dunning on usage |
| Onboarding friction | higher (customer needs a Twilio account) | lower |
| Time to sell | weeks | months |

**Recommended default: A (BYO) for launch, B later for a managed tier.** It
needs no usage billing, puts 10DLC registration and carrier fees with the
sender, and matches what the code already enforces (tenant account required,
no platform fallback). Build T-32 for A. Then credentials are stored with the
vault (`services/secret_vault.py`, MultiFernet) and webhooks point at the
existing signed endpoints.

## 4. Customer messaging cost model (Twilio US, list prices, verify)

| Item | Approx. list price | Under BYO (A) | Under subaccounts (B) |
|---|---|---|---|
| Outbound / inbound SMS segment | ~$0.008 + carrier fee ~$0.003–0.005 | tenant | LUXit, re-bill at ~2× |
| MMS | ~$0.02 + carrier fee | tenant | same |
| Local number | ~$1.15/month | tenant | LUXit |
| 10DLC brand (one-time) / campaign vetting | ~$4 / ~$15 | tenant | LUXit per tenant |
| 10DLC campaign monthly | ~$1.50–$10 | tenant | LUXit |
| Voice inbound / outbound | ~$0.0085 / ~$0.014 per minute | tenant | LUXit |
| Push (APNs/FCM) | free | n/a | n/a |
| Email | platform SMTP | LUXit (tenant email send is disabled by default, T-13) | same |

Example under B, 2,000 SMS/month: ~$25 carrier cost + ~$12 fixed. A $199 plan
absorbs it only with a cap. That is why B needs metering first.

## 5. Usage metering and billing safeguards

In place: server-side price lookup only (no client price IDs); webhook HMAC
+ 300 s tolerance; event idempotency; per-tenant event cursor (stale and
out-of-order events ignored); owner/admin-only billing actions; seat limits
include pending invites; free-plan caps; contact meter event per company per day.

Missing / recommended:
* **Contact meter install** (`deploy/billing/`, needs approval) + Stripe meter (`last`).
* **Spend alerts in Stripe** (Billing → Alerts) on the meter, for the operator.
* Under model B only: per-tenant SMS/minute counters and a monthly cap (block +
  notify at 100 %), before any tenant uses platform subaccounts.
* **Campaign send safety**: campaigns run in a web daemon thread (T-14). Move
  them to the worker before customers send large campaigns.

## 6. Legal (D-5, D-7: counsel review required)

Present: `/terms`, `/privacy`, `/sms-consent`, `/data-deletion` (updated 2026-04-26).
Missing: Acceptable Use Policy (spam, prohibited content, 10DLC rules),
billing/cancellation/refund terms (§2), DPA and subprocessor list (Stripe,
Twilio, OpenAI, hosting, email provider, PostHog if enabled), data retention
(30-day workspace retention, audit-log retention D-9). **Recommended:**
click-through Terms + AUP for Starter/Professional; order form for Enterprise.

## 7. Support and account recovery

Present: password reset by email (reset also verifies the email; revokes all
other sessions, including the phone app); email verification with resend;
team invitations; owner/admin role changes are audited; platform admin can
reopen a closed workspace.

Gaps / runbook (D-11):
* **Owner locked out and no mailbox access:** no self-serve path (by design).
  Support verifies identity out of band (billing email match + last invoice
  amount from Stripe), then a platform admin promotes another member or resets
  the owner's email. Every step leaves a `security_audit_event`.
* **Last owner leaves:** blocked (409) since 47de23c; the owner must transfer or close.
* **Support mailbox:** confirm `support@luxit.app` receives mail. Note that
  luxit.app has an MX record (checked 2026-11-06); confirm someone reads the mailbox.

## 8. Tenant deletion and data export

Done (a2f1a48): owner/admin export (ZIP of JSON lines, credentials excluded),
owner close, 30-day retention, platform reopen, operator purge (dry run by
default; rehearsed on a staging copy with real PostgreSQL, 15/15 workspaces).
See `docs/ops/WORKSPACE_CLOSURE_AND_EXPORT.md`. Individual-person deletion
requests still go through `/data-deletion` (manual).

## 9. Customer onboarding documentation

`docs/customer/GETTING_STARTED.md`: sign up → verify → workspace → plan →
team → contacts → segments → automations → billing → leaving.

## Owner decisions summary (recommended defaults)

| ID | Decision | Recommended default |
|---|---|---|
| D-1 | Trial | none; free plan + upgrade |
| D-2 | Prices | $199 / $499, overage per contact on a `last` meter |
| D-3 | Grace | 14 days for billing **and** phone license |
| D-4 | Refunds | none prorated; cancel at period end |
| D-5 | Agreement | click-through for self-serve; order form for Enterprise |
| D-6 | Messaging | BYO Twilio at launch; subaccounts later |
| D-7 | Legal pages | counsel review of Terms, AUP, DPA, subprocessors |
| D-9 | Audit retention | 4 years, then scheduled delete |
| D-10 | Backups / alerts | off-site bucket + gpg key generated off-host; email + SMS alert to on-call owner |
| D-11 | Support | monitored support mailbox, 1-business-day response |
| new | Workspace retention before purge | 30 days |
