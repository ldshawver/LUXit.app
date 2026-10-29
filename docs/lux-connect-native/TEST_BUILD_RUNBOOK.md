# LUXit Phone: first test builds, step by step

Based on commit `ab91b6c` (`feat/lux-connect-native`). Written 2026-10-09. The full account details are in CREDENTIALS.md. This page gives the **order** and **who does each step**.

Ground rules for every step:
- Production is not touched: no production deploys or restarts, and no production Twilio, Messaging Service or 10DLC changes.
- Nothing is bought without approval.
- `ITSAppUsesNonExemptEncryption` stays unset (EXPORT_COMPLIANCE.md). No exemption is claimed and no compliance code is set.
- France stays out of the first public release.

## Where things stand (checked 2026-10-09, read-only)

| Item | State |
|---|---|
| Bundle IDs in `mobile/app.config.ts` | production `com.luxit.connect`, staging `com.luxit.connect.staging`. **Not confirmed against App Store Connect.** |
| `eas.json` | four profiles valid. `submit.*.ios.ascAppId` = `SET_ME` |
| Expo project | none: no `EAS_PROJECT_ID`, no `EXPO_OWNER`, no `EXPO_TOKEN` |
| Staging server | runs `c2eda12`: native API only, **without** the phone features, permission and allowlist-gate commits (`4b097c4`…`ab91b6c`). `LUXIT_TWILIO_MODE=disabled`. `h2` and `google-auth` are installed |
| Staging push variables | **none set**: no `TWILIO_PUSH_CREDENTIAL_SID_*`, `LUXIT_APNS_*` or `LUXIT_FCM_*`. The app therefore shows "Outgoing calls only" |
| Twilio push credentials for LUX Connect | none. The existing `CR…` credentials belong to Talkyto and must not be reused |
| Firebase project / `google-services.json` | none |
| Staging SMS | outbound blocked by the gate. Inbound to +18302591310 goes to **production** through Messaging Service `MGa8e0cd…` |

Deploying `ab91b6c` to staging needs **no migration**. The only SQL in the diff is a one-off repair script under `scripts/`, which nothing runs automatically. The branch also contains the email-suppression commits that are already on production (`9aa5faa`, `645dd65`), so staging would catch up with production on those.

## Order of work

Each step names who does it. "You" means you are signed in to the account in question. "Claude" means I can do it once the input exists.

| # | Step | Who | Unblocks |
|---|---|---|---|
| **1** | **Confirm the production bundle ID** in App Store Connect (*Apps → app → App Information → Bundle ID*), or in Apple Developer *Identifiers* if no app record exists yet. Tell me the staging ID you want. If either differs from the current IDs, I change `VARIANTS` in one commit. | You, then Claude | everything below: App IDs, push certificates, Firebase apps and Twilio credentials are each tied to the ID |
| 2 | **Expo:** create or choose the account/organization. Run `cd mobile && eas login && eas init`, or give me an `EXPO_TOKEN` (store it outside git) and the owner name. | You | builds |
| 3 | **Apple Developer** (paid membership, Admin role): <br>• App IDs for both bundle IDs with *Push Notifications* enabled. <br>• One **VoIP Services certificate** for each App ID, exported as .p12. <br>• One **APNs Auth Key (.p8)**, plus its Key ID and your Team ID. <br>• Register each test iPhone's UDID (`eas device:create`). | You | iOS builds; iOS incoming calls and alerts |
| 4 | **Firebase:** create a project, add Android apps for both package names, download `google-services.json`, and create a service account with the *Firebase Cloud Messaging API Admin* role (JSON key). | You | Android incoming calls and alerts |
| 5 | Load `google-services.json` into EAS (`eas env:create --name GOOGLE_SERVICES_JSON --type file …`; exact command in CREDENTIALS.md). Fill in `EAS_PROJECT_ID` and the owner. | Claude (with step 2) or you | Android builds with push |
| 6 | **Twilio push credentials** (free, but created on the shared account, so it needs your approval): `LUX Connect iOS staging` (APN, sandbox=false, from the staging VoIP certificate) and `LUX Connect Android` (FCM, from the step 4 service account). They are only *referenced* by staging's environment. No number, webhook, Messaging Service or TwiML App changes. | You create them, or approve and I create them from files you place on the server | incoming call ringing |
| 7 | **Staging update** (needs approval): <br>• deploy `ab91b6c` to `/srv/lux-email-bot-staging`; <br>• set `TWILIO_PUSH_CREDENTIAL_SID_IOS`, `TWILIO_PUSH_CREDENTIAL_SID_ANDROID`, `LUXIT_APNS_KEY_FILE/KEY_ID/TEAM_ID`, `LUXIT_APNS_TOPIC=<staging bundle id>` and `LUXIT_FCM_SERVICE_ACCOUNT_FILE` (key files mode 600 under `/etc/luxit-staging/`); <br>• restart `lux-email-bot-staging` only. <br>Check: `/api/mobile/v1/me` shows `push.apns`, `push.fcm` and `calling.incoming` as available. | Claude, after approval | device testing |
| 8 | **First builds:** `eas build --profile staging-dev --platform android` (APK, no Apple steps needed). Then `eas build --profile staging-dev --platform ios`, run **interactively** and signed in to Apple so EAS creates the ad hoc profile with the registered iPhones. If EAS asks "only uses standard/exempt encryption?", answer **No**: the key stays unset. | You (interactive iOS); Claude can run Android with `EXPO_TOKEN` | device testing |
| 9 | **Staging SMS decision.** <br>(a) Approve a new staging number and its own staging Messaging Service and campaign (a purchase), or <br>(b) approve `allowlist` mode with your test phones (outbound only; **do not reply** from them). <br>Without either, SMS tests use the push-only substitute below. | You | real SMS / MMS on a device |
| 10 | Physical-device run (DEVICE_ACCEPTANCE.md, "First session"). | You on the phones; I watch the staging logs and Twilio records | acceptance |

The Android APK (step 8) only needs steps 1, 2 and (for incoming calls) 4–7. Android can run ahead of iOS while the Apple steps are pending.

TestFlight (`testflight-staging`) also needs the App Store Connect record and its `ascAppId` in `eas.json`. Each upload then waits for the export questions to be answered in App Store Connect, because the key is unset: Yes / Standard algorithms in addition to the OS / France No. Ad hoc `staging-dev` builds don't need any of that, so use them first.

## Testing SMS alerts without a real SMS (until step 9)

To test incoming-text **notifications**, I can post a **signed synthetic inbound webhook** to staging, from a made-up customer number to +18302591310. This was already used for S9–S11.
- What it tests: the stored message, the real APNs/FCM alert to the handset, opening the thread, and the badge.
- What it doesn't test: carrier delivery.

Record these results as "synthetic inbound", not "SMS received". Voicemail and missed-call alerts need nothing extra: a real call to +18302591310 that nobody answers goes to staging's voicemail.
