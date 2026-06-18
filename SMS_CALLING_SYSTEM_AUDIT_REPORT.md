# SMS and Calling System Audit Report

Date: 2026-06-17

## Root causes identified

1. **Conversation-thread replies**
   - The PWA thread API already accepts a plain `body` from the active conversation, but the desktop `/twilio/send` API still required both `to` and `body` even when a `conversation_id` was supplied.
   - This made the backend less tolerant than the UI and kept command-style workflows (`reply +1... message`) embedded in forwarded SMS instructions.

2. **Per-character message rendering**
   - The PWA renderer had a defensive JavaScript normalizer for payloads that arrive as one character per line, but message bubbles still allowed aggressive wrapping in narrow/flex layouts.
   - The bubble CSS now preserves message text while avoiding character-by-character wrapping.

3. **Google Contacts integration**
   - OAuth/token helpers and sync endpoints existed.
   - Sync only upserted contacts when a Google number matched an existing Twilio conversation, so PWA contact search and future inbound SMS lookup did not benefit from the full Google contact cache.
   - Existing conversations also did not refresh their display names when a matching contact was added later.

4. **Phone icon/calling workflow**
   - The PWA already included a native dial pad, manual number entry, native contact picker support, recent/missed/voicemail call lists, and Twilio outbound call endpoints.
   - Remaining risk was clarity: outbound calls must always use the tenant Twilio `from_phone` as caller ID and should not push users toward external apps.

5. **Outbound calling**
   - The native PWA call paths create Twilio calls using the configured business number (`from_=ta.from_phone`).
   - If a forwarding number is configured, Twilio calls the employee first and then bridges to the customer; otherwise Twilio calls the customer directly. In both cases, the business number is the caller ID.

6. **SMS send pipeline**
   - Thread lookup existed, but desktop send updated conversation metadata before Twilio dispatch. A failed Twilio send could make the thread look successful.
   - Error handling/logging now keeps metadata updates behind a successful provider response and logs failed sends with company/conversation context.

## Completed repairs

- Allowed `/twilio/send` to send a plain message using only `conversation_id` + `body`, resolving the destination number from the thread when `to` is omitted.
- Moved desktop conversation metadata updates until after successful Twilio dispatch and added failure logging.
- Normalized conversation lookup across phone-number variants so existing threads are reused regardless of phone formatting.
- Refreshed existing conversations with CRM/Google contact names when a contact match is found after the thread already exists.
- Populated the local Contact cache from all synced Google Contacts phone numbers, not only numbers with existing Twilio conversations.
- Set cached Google contact display names on the Contact row to support PWA contact search.
- Hardened PWA message bubble rendering to avoid one-character-per-line wrapping.

## Affected files

- `twilio_sms.py`
  - Conversation lookup and contact-name resolution.
  - Desktop SMS send API behavior, dispatch ordering, and error logging.
- `services/google_contacts.py`
  - Google Contacts cache population and contact upsert behavior.
- `templates/inbox_pwa/index.html`
  - Message bubble rendering and pathological SMS body normalization.

## Verification notes

- OAuth connection: `services/google_contacts.py` provides auth URL generation, code exchange, token storage, refresh, status helpers, and disconnect.
- Sync job execution: PWA and Twilio settings routes call `sync_contacts(user_id, company_id)`; this now populates all Google phone contacts into the local Contact cache.
- Contact cache population: all `phone_map` entries are upserted into `Contact` with `source="google_contacts"`.
- Contact lookup in PWA: `/api/inbox/contacts/search` queries Contact rows and then conversations.
- Incoming SMS name resolution: `_get_or_create_conversation()` now checks normalized phone variants, Contact rows, and previously synced conversation names for both new and existing conversations.
- Outbound SMS: `_send_sms()` dispatches through Twilio, creates `TwilioMessage`, logs success/failure, and `/twilio/sms/status` updates delivery status by Twilio SID.
