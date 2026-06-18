# Marketing Segments and Suppression

Admins, managers, and editors can create, edit, copy, activate/deactivate, and manage segment contacts. Segment deletion removes segment memberships/rules for that segment only; contacts are preserved. Copying creates a new segment named with a `Copy` suffix and duplicates description, type, category, match mode, triggers, conditions, actions, dynamic/static state, and active state, but does not copy audit history or send campaigns.

Segment contacts can be manually added/removed one at a time or in bulk. Membership rows store source (`manual`, `dynamic_rule`, `imported`, `woocommerce`, `sms_opt_in`, `email_opt_in`, `affiliate`, `lux_verified`), the user and timestamp that added or removed them, and permanent exclusion metadata. Dynamic segment refreshes must honor `is_excluded=true` so rules cannot immediately re-add permanently excluded contacts.

## Do Not Market precedence

Marketing suppression is contact-level and wins over segment membership, campaign enrollment, automation rules, AI suggestions, purchaser, affiliate, and LUX verified status:

1. `do_not_market` blocks all marketing email and SMS.
2. `do_not_email` blocks marketing email.
3. `email_unsubscribed` blocks marketing email.
4. `do_not_sms` blocks marketing SMS.
5. `sms_opted_out` blocks marketing SMS.
6. STOP/opt-out compliance always wins over segment membership.

Transactional messages are not blocked unless a sender has not separated transactional from marketing delivery.

Campaign send flows log skipped contacts with reasons: `do_not_market`, `do_not_email`, `email_unsubscribed`, `do_not_sms`, `sms_opted_out`, `missing_email`, `missing_phone`, `invalid_phone`, and `tenant_mismatch`.

## Deployment migration note

Production deploys run SQL files from `migrations/*.sql` in `.github/workflows/push-to-production.yml` when `psql` and `DATABASE_URL` are available. The segment suppression migration uses `ADD COLUMN IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` so repeated deploys can safely re-run it while ensuring required schema columns exist.
