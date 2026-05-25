---
name: Twilio outbound call from PWA inbox
description: How the click-to-call feature works in the SMS inbox PWA.
---

**Frontend:** Green phone icon button (`#callBtn`, class `thread-act-call`) in the thread header. `openConversation()` sets `state.activeConvPhone` and `callBtn.href = 'tel:' + from_number`. This is a native `<a>` tag so it triggers the device dialer on mobile.

**Backend API:** `POST /api/inbox/conversations/<id>/call` in `inbox_pwa.py` (`place_outbound_call`).
- Looks up `TwilioConversation` scoped to the company.
- Gets `TwilioAccount` via `_get_twilio_account(company.id)`.
- Reads `forward_to` from POST body or falls back to `ta.call_forward_to`.
- If `forward_to` set: calls agent's phone first; TwiML bridges to customer when agent answers.
- If no `forward_to`: calls customer directly (voicemail/test use case).

**TwiML bridge:** `GET /twilio/voice/outbound-twiml` in `twilio_sms.py` (`outbound_call_twiml`).
- Accepts `?to=CUSTOMER_PHONE&caller=BUSINESS_PHONE` query params.
- Returns `<Say>` + `<Dial callerId="...">...</Dial>` TwiML.
- Route name for `url_for`: `"twilio.outbound_call_twiml"`.
