# LUX Connect texting: US A2P 10DLC and onboarding

## Why it matters

US carriers filter application-originated texts (anything sent through Twilio's API, which includes every LUX Connect text) from 10-digit long codes unless the number belongs to an **approved A2P 10DLC campaign** under a registered **brand**. Toll-free numbers instead need an approved **toll-free verification**. Unregistered traffic is blocked or heavily filtered (Twilio error 30034 and similar), and the customer sees "sent" messages that never arrive.

A newly assigned number therefore **cannot be assumed to text immediately**, even though it can make and receive calls at once.

## What the account has today (read-only check 2026-10-09)

- 3 brands, all APPROVED: 2 STANDARD, 1 SOLE_PROPRIETOR.
- Messaging Service `MGa8e0cdea…` ("Default Messaging Service for Conversations") has a VERIFIED US A2P campaign. It contains `+18302591310` (staging), `+19165989519` (production) and `+18664868340`.
- `+18664868340` is **toll-free**. A 10DLC campaign does not cover it, and no toll-free verification could be read (the account lacks API access to that list, Twilio 20007). The check records it as `toll_free_pending` until verified.

## What the build does

- `phone_number_messaging_registration` holds one row per number with status `registered` | `pending` | `not_registered` | `toll_free_verified` | `toll_free_pending` | `unknown`.
- `scripts/check_messaging_registration.py` is **read-only against Twilio** and fills that table. Run it after any number or campaign change, and on a schedule (daily is fine).
- `POST /api/mobile/v1/messages` refuses with `422 messaging_not_registered` when the number's status is `not_registered`, `pending` or `toll_free_pending`. **`unknown` (no check yet) never blocks**, so existing behaviour is unchanged until the check runs. The app shows "Your number isn't approved for business texting yet. Calls still work."
- STOP / START / HELP handling, opt-out suppression and consent purpose are unchanged. The app sends through the same `sendConversationSms` path, which refuses STOP'd conversations.

## Customer onboarding flow (proposed)

1. **Number assignment (admin):** pick an existing company number (Numbers screen or web hub). The app shows *Calls: ready* immediately and a *Texting* status per number.
2. **Brand:** the company's legal entity must be a registered brand. Sole proprietors use the Sole Proprietor brand type, which allows 1 campaign, 1 number and lower throughput.
3. **Campaign:** register a "Mixed" or "Customer Care" campaign describing conversational two-way texting with customers, with:
   - sample messages;
   - an opt-in description (customers text the business first, or opt in through the existing hosted promotional opt-in page for marketing);
   - STOP / HELP keywords (already handled).
4. **Attach the number** to the Messaging Service that holds the approved campaign, then run the check script. Status becomes `registered` and texting is enabled in the app.
5. **Toll-free numbers** go through toll-free verification instead. They stay `toll_free_pending` until approved.

Timelines are outside our control: brand vetting from minutes to days, campaign review typically 1–7+ business days.

## Not done (needs authorization)

- No brand, campaign or number was registered, moved or attached. All Twilio calls in this work were reads, plus test voice calls on the staging number.
- Promotional / marketing texting from the app is not offered. The app texts within existing conversations and to numbers the user enters, under the same consent rules as the PWA.
