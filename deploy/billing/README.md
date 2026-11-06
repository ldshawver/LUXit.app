# Contact-overage metering

Stripe bills contacts above the plan allowance (Starter 2,500, Professional
50,000; `services/stripe_billing.INCLUDED_CONTACTS_BY_TIER`) through the
metered price `luxit_contacts_usage_monthly`.

Stripe removed legacy usage records in API 2025-03-31, so the price must be
attached to a **Billing Meter**:

| Meter field | Value |
|---|---|
| event name | `luxit_contacts_overage` (or set `LUXIT_CONTACTS_METER_EVENT`) |
| aggregation formula | **`last`** (a daily report replaces the previous value; `sum` would multiply the bill) |
| customer mapping | `stripe_customer_id` (default) |
| value key | `value` (default) |

`scripts/acceptance/stripe_testmode_preflight.py` refuses a meter that is
missing, inactive or not `last`.

Install (needs approval; staging first):

```bash
python scripts/report_contact_usage.py            # dry run: counts + overage per workspace
cp deploy/billing/luxit-contact-usage.{service,timer} /etc/systemd/system/
# set WorkingDirectory to the current release dir, then:
systemctl daemon-reload && systemctl enable --now luxit-contact-usage.timer
```

Same-day re-runs are deduplicated by Stripe (event identifier
`luxit-contacts-<company>-<YYYY-MM-DD>`).
