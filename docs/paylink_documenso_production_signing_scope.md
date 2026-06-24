# PayLink/Documenso production signing fix scope

This change set is intentionally scoped to the PayLink/Documenso production
contract-signing incident. It should be reviewed and approved only for the
signing flow and deployment guardrails needed to support that flow.

**Review status:** approved for LUXit infrastructure hardening and
Documenso/deployment safety improvements only. Communications production
readiness remains incomplete and open.

## In scope

- Documenso API-key and webhook-secret startup validation without printing
  secret values.
- Disabling or failing send-for-signature when `DOCUMENSO_API_KEY` is missing.
- Verifying Documenso webhook signatures when `DOCUMENSO_WEBHOOK_SECRET` is
  configured.
- Idempotently persisting Documenso document/envelope IDs, signing URLs,
  recipient IDs, and signature request rows for contract signing.
- Avoiding production SQL failures seen while notifying contract activation
  recipients.
- Adding narrow schema-drift compatibility only where production signing or its
  notification path trips over missing preference columns.
- Preventing deploy/startup regressions that block the PayLink app from serving
  the signing flow, including missing frontend index paths and duplicate port
  listeners.

## Explicit non-goals

This PR must **not** be approved as solving broader LUXit communications issues,
including but not limited to:

- PWA feature completeness.
- SMS sending or campaign reliability.
- After-hours routing behavior.
- Push notification delivery quality.
- Sound, vibration, unread reminder, or alert UX behavior.

The migration includes compatibility columns for production schema drift only;
that is not a product-level fix or acceptance claim for PWA, SMS, after-hours,
push, sound, or alert systems.

## Communications workstream status

This infrastructure-hardening PR does **not** complete the LUXit communications
workstream. Communications production readiness requires a separate PR with live
VPS/Twilio/database/browser evidence for at least the following unresolved areas:

1. After-hours SMS behavior, including an 11:00 AM inbound SMS producing the
   configured after-hours reply, Twilio accepting the outbound message, same
   conversation-thread persistence, and `auto_responded=true` on inbound rows.
2. A canonical after-hours settings audit proving there is exactly one editable
   source, no duplicate/stale editors or APIs, and clear UI load/save paths.
3. Push notification implementation and validation for SMS, missed calls,
   voicemail, and unread conversations across iPhone Safari/PWA, Android Chrome,
   installed PWA, backgrounded app, and locked-screen states.
4. Business-hours sound/vibration reminders every 60 seconds for unread
   conversations until read, replied to, or auto-responded, with suppression
   outside business hours while preserving after-hours SMS auto replies.
5. Missed-call ringing, alerts, voicemail alerts, badge counts, and PWA wake
   behavior.
6. Production evidence from `journalctl -u luxit -f`, Twilio logs, database
   rows, and screenshots/video demonstrating the expected behavior.

Do not mark communications complete from this PR.
