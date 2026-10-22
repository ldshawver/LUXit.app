# Development root-cause report: PWA inbound communications

Date: 2026-07-24. Scope: local development checkout only. No production, DNS,
service, database, or Twilio Console setting was changed.

## Trace findings before the repair

* **Device registration / HTTP 401:** both push endpoints correctly derive the
  user and company from the authenticated Flask session and reject anonymous
  callers. The Inbox request helper sends same-origin credentials and CSRF, but
  a stale/expired installed-PWA session therefore returns the observed 401. The
  UI redirected immediately rather than retaining a useful authentication
  diagnosis. Server persistence and endpoint idempotency were already present.
  More importantly, the worker was served from `/static/sw.js` without a root
  scope, so its default `/static/` scope could not control `/app/*` pages.
  The reconciliation code also compared an endpoint suffix which the debug API
  did not return, so every launch treated a valid server record as mismatched.
* **Inbound SMS:** the signed Twilio webhook resolves the receiving line before
  the tenant, persists by unique MessageSid, runs compliance handling before
  general delivery, commits, and then invokes tenant/line permission-filtered
  push and SSE helpers. Web Push cannot dispatch with zero active subscriptions;
  that registration failure explains the missing closed-PWA alert. Live-page
  SSE and sound deduplication were already implemented.
* **Inbound Voice:** the signed webhook resolves the tenant-owned receiving
  number, writes CallSid-idempotent state, emits notification/SSE events, and
  returns Client TwiML for the same tenant identity. The Calls page attached SDK
  listeners before `Device.register()`, but initialization automatically asked
  for microphone access during page load. Browsers require that permission and
  audio unlock to follow a user gesture, so registration stopped before the
  browser could receive an invite. The Calls page also lacked an explicit
  browser-calling enable/dial workflow, while the Inbox displayed the feature as
  unavailable.
* **External evidence:** this checkout has no configured git remote and exposes
  no development Twilio credentials or authenticated Twilio Console session.
  Consequently Twilio debugger/request logs and real provider webhook settings
  could not be inspected safely from this environment. Application journal
  access was not used because no checkout-specific development service was
  identified.

## Development-only configuration still required

Configure the development Twilio number's incoming voice webhook as HTTPS POST
`<DEV_HTTPS_ORIGIN>/twilio/voice/inbound`, messaging webhook as HTTPS POST
`<DEV_HTTPS_ORIGIN>/twilio/sms/inbound`, and the development TwiML App Voice URL
to the repository's canonical browser-outbound TwiML route. Set (without
printing values) `TWILIO_ACCOUNT_SID`, `TWILIO_API_KEY`, `TWILIO_API_SECRET`,
`TWILIO_TWIML_APP_SID`, `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, and
`VAPID_SUBJECT`. The number `+19165989519` must exist as an active tenant-owned
`TwilioPhoneNumber`, with browser calling and the intended routing permissions
enabled. Never point these paths at production during acceptance testing.

## Platform limits

An installed browser PWA is not iOS CallKit. A fully terminated PWA may show Web
Push, but cannot guarantee a live Twilio Voice SDK socket or native incoming-call
screen. Notification sound, background execution, microphone prompts, and
ringtone autoplay remain controlled by the browser/OS. A prior user gesture is
required to unlock audio, and the UI reports when the SDK is not registered.
