# Release candidate 2026-10-29 — SaaS + differentiators + LUX Connect native backend

Branch `release/saas-native-20261029` = `219fb31` (SaaS launch line + differentiators M1–M7)
merged with `daa3d68` (native backend: /api/mobile/v1, forwarding safety 9311cec,
live-session ringing, push-token storm protection). Local only; not pushed, not deployed.

## Fixes made while integrating

| Commit | Fix | Severity |
|---|---|---|
| 8b483e6 | T-16 refusal inside `_voice_token_gate` returned the wrong shape after the merge → PWA voice token, native `/me` and `/voice/token` answered 500 for any workspace without a platform sender | High (merge-only; never shipped) |
| b5ef14a | Native sign-in skipped the email-verification gate and wrote no T-26 audit events | Medium |
| efdf732 | Change-password (2 routes) left every other session alive and was not audited; phones whose sessions were ended by a password reset/change kept **ringing tenant calls** until refresh expiry | High (reset path exists on prod 1c56fbe for web sessions; native ringing only once native ships) |
| af22312 | Test positive control (handset reassignment) | — |

Test gotcha found: native tests share one app context, so a leaked `g._login_user`
turns the voice webhook into a 302 and "identity not in TwiML" passes vacuously.
The exclusion test now clears `g` and requires real TwiML (negative control: fails without the fix).

## Tests (2026-10-29)

- Native API 95 pass; native + journey + voice set 161/1 before fixes, all green after.
- T-16 sender isolation, signup, security audit, P0/positive-control security suites: green
  (4 `test_deploy_migration_workflow` failures = recorded baseline).
- Full batched suite: batches b00–b09 = 53 failed / 1031 passed / 81 skipped;
  **0 failures outside the 1c56fbe-lineage baseline** (`rc-20261029-failures-b00-b09.txt`).
  **b10–b15 not run** — stopped by the host memory reaper during b10. Still owed before staging.
- Cross-tenant PK-fetch scan: no new unscoped fetch vs prod (1 hit = signature-verified webhook).

## Staging

Package `/root/luxit-ops-backups/20261029-staging-release-candidate/` (deploy.sh <full-sha>,
verify.sh, rollback.sh): guards, luxstaging backup, 7 additive migrations (staging ledger 42 rows,
0 checksum drift), `zzz-release-rc-*` drop-in, nginx untouched. **Not run** — waits for b10–b15.
