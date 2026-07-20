# Contact identity runtime audit (2026-07-20)

## Traced ownership

* `twilio_sms.inbound_sms` owns both `POST /twilio/sms/inbound` and its legacy
  `POST /twilio/sms` alias. It resolves the receiving `TwilioPhoneNumber` and
  `TwilioAccount`, verifies the signature, and commits the inbound
  `TwilioMessage` before best-effort contact enrichment, replies, forwarding,
  SSE, or push delivery.
* `_get_or_create_conversation` resolves a company-scoped contact through
  `contact_audience.upsert_contact_from_source`, synchronizes contact points,
  applies the MyOrder tag rule, and links `TwilioConversation.contact_id`.
* `services.phone_normalization` is the canonical libphonenumber boundary.
  Google, Audience, deduplication, campaigns, and Twilio compatibility wrappers
  now delegate to it.
* Google OAuth tokens are user-owned; `GoogleContactConnection` binds that
  owner to a company. `google_contacts.sync_contacts` refreshes access tokens,
  follows every People API page, and stores company/user/connection-scoped
  lookup rows. The application scheduler is started by the application process;
  deployments must ensure exactly one scheduler-enabled process.
* `inbox_pwa._conv_to_dict` performs a fresh tenant-scoped contact lookup when
  serializing a conversation, so a sync or confirmation changes its display
  without recreating the conversation.

## Root causes found in the implementation

1. Google used a second permissive, regex-only phone normalizer while Audience
   and contact intelligence used libphonenumber. Invalid values could be
   indexed and equivalent inputs could take different paths.
2. The People fetcher indexed all phone fields but silently discarded a later
   Google person when two resources shared one normalized phone. Consequently
   ambiguity could never reach the lookup table and the first person appeared
   reliable.
3. The inbound route created a Contact but never consulted
   `GoogleContactLookup`, never set an explicit identity state, and had no
   identity collection/confirmation state machine.
4. Conversation JSON refreshed a name, but exposed neither Google-match nor
   identity status. The Audience table omitted identity state and acquisition
   timestamp and provided no required identity filters.
5. Existing contact-intelligence jobs repair contact rows, but the deployment
   workflow had no evidence that a full Google sync and historical phone-source
   backfill were actually executed. Schema success or `/contacts` HTTP 200
   therefore cannot establish data completion.

## Operational boundary

This repository change does not access production credentials, send controlled
SMS messages, create a PostgreSQL backup, run production migrations/backfills,
or restart services. Those ledgered deployment steps must be performed by an
authorized operator and their aggregate evidence attached to the release.
