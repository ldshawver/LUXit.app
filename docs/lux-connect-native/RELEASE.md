# LUX Connect native: release, production and rollback

**Status: not production-ready.** Native calling and messaging have not been validated on physical devices (DEVICE_ACCEPTANCE.md). Nothing here has been deployed to production.

## 1. TestFlight (iOS) and Google Play internal testing

Prerequisites: everything in CREDENTIALS.md, and `cd mobile && npm ci`.

```bash
cd mobile
npx eas login                                    # or EXPO_TOKEN in CI
npx eas init                                     # once; sets EAS_PROJECT_ID
npx eas secret:create --type file --name GOOGLE_SERVICES_JSON --value ./google-services.json
npx eas credentials                              # let EAS create/manage the iOS dist cert, profiles, Android keystore
npx eas build --profile testflight-staging --platform all
npx eas submit --profile testflight-staging --platform ios       # TestFlight
npx eas submit --profile testflight-staging --platform android   # Play internal track
```

- First Play upload: Play requires the first AAB to be uploaded by hand in Play Console. Later uploads can use `eas submit`.
- Before inviting testers, check that `/api/mobile/v1/me` on staging reports `calling.incoming: true` and `push.apns` / `push.fcm` true for the build's platform.
- Bump `version`, `ios.buildNumber` and `android.versionCode` in `app.config.ts` for every store upload.

## 2. Production deployment requirements (needs explicit authorization)

Backend, using the 2026-09-28 / 10-06 release pattern (immutable release dir + `zz-release-*.conf` drop-in, single restart):

1. Decide the base. Production runs `645dd65`. The native branch sits on top of `32814a7` (the PWA phone overhaul, itself unreleased), so shipping the native backend also ships the PWA overhaul. Authorize both together, or ship the overhaul first.
2. Back up the production DB, then apply `migrations/20261009_lux_connect_native.sql` through `scripts/apply_migrations.py` (new tables only). The re-run must be a no-op.
3. `pip install h2==4.3.0` into the release venv (APNs only).
4. Set the production env from CREDENTIALS.md (production Push Credential SIDs, APNs topic `com.luxit.connect`, FCM service account). Without them production behaves exactly as today for the PWA, and the app reports "Outgoing calls only".
5. Restart `lux-email-bot` once. **Do not** restart the Tuya worker, and do not touch weekly contact dedupe.
6. Smoke test:
   - `/health` returns 200;
   - `/api/mobile/v1/config` returns 200;
   - `/api/mobile/v1/me` without a token returns 401;
   - a PWA call still rings and is answered;
   - inbound SMS is still stored;
   - STOP is still enforced.
7. Run `scripts/check_messaging_registration.py` (read-only Twilio, writes only the new table).

The app's `production` profile points at `https://luxit.app` and the `com.luxit.connect` bundle.

## 3. Rollback

| What | How | Effect |
|---|---|---|
| Backend | Remove the drop-in, `systemctl daemon-reload`, restart → previous release dir (`645dd65`) | `/api/mobile/v1` disappears; native apps show "sign in required" / offline; PWA unaffected |
| Native ringing only | Unset the `TWILIO_PUSH_CREDENTIAL_SID_*` env vars and restart | New tokens have no incoming; already-registered handsets keep ringing until the device is signed out or revoked. To stop them at once, set every `mobile_device.status='signed_out'`; `ring_pwa` then never dials them |
| Alert push | Unset the `LUXIT_APNS_*` / `LUXIT_FCM_*` env vars | No native alert pushes; calls unaffected |
| Schema | Leave in place (additive, unused after rollback). Drop the five `mobile_*` / `phone_number_messaging_registration` tables only if abandoning the feature | none |
| App | Stop the TestFlight / Play rollout; `LUX_CONNECT_MIN_APP_VERSION` can be raised later to force updates | |

## 4. Staging state left by this work

- `/srv/lux-email-bot-staging` was detached at the staging branch `staging/lux-connect-native` and restarted; the migration is applied. Rollback tag: `staging-pre-lux-connect-native-20261009T005351Z` (= `3574ec7`). Env backup: `/etc/luxit-staging/staging.env.bak.lux-connect-native.20261009T005351Z`.
- QA user `lux-connect-qa@staging.luxit.app` (id 47) was archived after testing; its device rows are signed out.
