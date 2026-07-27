# PWA inbound communications development acceptance

## Development-only configuration

Configure the development Twilio number's HTTPS webhooks (HTTP `POST`) to the
development LUX origin:

- Messaging: `/twilio/sms/inbound`
- Voice: `/twilio/voice/inbound`
- Voice status: `/twilio/voice/status`

Set `TWILIO_STRICT_SIGNATURE=1`, the development Twilio API key/secret and
TwiML Application SID, and a development-only VAPID key pair and subject. Do
not copy production credentials into development. The browser must load LUX
from the same HTTPS origin used by these paths. `/sw.js` must return
`Service-Worker-Allowed: /` and must not be cached.

## Root causes repaired

- The worker was served below `/static`, whose default scope cannot control
  `/app`; it is now served at the origin root with explicit scope and safe
  upgrade headers.
- Flask-Login sessions use `_user_id`; PWA APIs now use the canonical current
  user and same-origin credentialed browser requests.
- Voice forwarding could override `ring_pwa`; routing now follows the selected
  route and honors the corresponding settings destination.
- Voice identities can now be tenant-, user-, and device-specific. Approved
  active devices are dialed independently; an unregistered supplied device key
  cannot obtain a device Voice token.
- The disabled Wi-Fi Calling placeholder was replaced by an enabled entry into
  the existing Twilio Voice dialer. This is browser/PWA VoIP and does not alter
  carrier-level Wi-Fi Calling.

## Manual acceptance matrix

| Runtime | Open | Background | Fully closed | Expected limitation |
| --- | --- | --- | --- | --- |
| Android Chrome installed PWA | SDK call UI, audio and in-app SMS alert | Web Push; Voice SDK depends on process survival | Web Push opens LUX; live Voice invite requires app/SDK registration | No native CallKit; OS may stop the browser |
| iOS Home Screen web app | SDK call UI after user gesture and mic permission | Web Push on supported iOS versions | Push can open LUX; cannot answer before the SDK starts | No native CallKit and no guaranteed background Voice socket |
| Desktop Chromium | SDK call UI and notifications | Notifications and registered SDK while browser runs | Web Push when browser platform permits | Browser/OS notification policy controls sound |
| Safari/Firefox desktop | Feature-detect and show actionable status | Browser-dependent | Browser-dependent | Twilio Voice/Web Push support varies |

For each supported device, verify notification permission, service-worker
scope, active subscription count, test push receipt/click-through, one inbound
SMS event/sound, inbound Answer/Mute/Hang Up, outbound caller ID, two-company
isolation, and the same checks after restarting only the development service.

## Platform boundary

A PWA cannot promise native iOS CallKit behavior, wake a terminated Voice SDK
for an immediately answerable call, or override OS Focus/sound policy. Web Push
can alert and open/focus LUX; two-way calling begins after the authenticated
page initializes and registers the Twilio Voice SDK.
