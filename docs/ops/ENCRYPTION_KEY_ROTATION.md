# ENCRYPTION_MASTER_KEY: containment and rotation (P0-9)

## Finding (verified 2026-10-31, read-only)

- `/etc/systemd/system/luxit.service` (legacy, mode 0644) sets `ENCRYPTION_MASTER_KEY`
  and `SESSION_SECRET` inline. Any local account can read them:
  `sudo -u nobody systemctl show luxit.service -p Environment` printed the key.
- That key is the **production** key: same fingerprint in the running prod web
  process, the Tuya/durable-scheduler worker, and the legacy process. Staging and
  dev use different keys.
- The host has four non-root login accounts, so treat the key as disclosed.
- The legacy service is unproxied (loopback :8000, only manual curls since July)
  but still runs an hourly "APP Agent" scheduler against the production database.
- `SecretVault` also had two silent failure modes, now fixed in the release
  branch: a malformed key fell back to a random key (new secrets written under a
  key that dies with the process), and a missing key logged the generated key.
  Production (`FLASK_ENV=production`) now refuses to start instead.

The vault protects provider credentials, Twilio sub-account SIDs and tokens,
OAuth tokens (Facebook, X, TikTok), company secrets and advanced-config
secrets. `scripts/rotate_encryption_key.py` scans every text/JSON column instead
of using a list, so values inside JSON documents are rotated too.

## Step 1: containment (needs approval, no restart of prod)

`/root/luxit-ops-backups/20261031-legacy-luxit-retirement/apply.sh` stops,
disables, removes and masks `luxit.service`, keeping a 0700 copy for
`rollback.sh`. It aborts if nginx proxies to :8000 or if prod runs from the
legacy directory. It does not touch `lux-email-bot.service` or its venv.

## Step 2: rotation (needs approval; restarts prod web + worker)

Requires the release branch, which adds `ENCRYPTION_MASTER_KEY_PREVIOUS` (MultiFernet).

1. Back up the production DB (`luxit-db-backup.service`, or the deploy package's dump).
2. `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
3. In `/etc/luxit/prod.env` (0600) and the worker's environment:
   `ENCRYPTION_MASTER_KEY=<new>` and `ENCRYPTION_MASTER_KEY_PREVIOUS=<old>`.
   Restart `lux-email-bot` and `luxit-tuya-notification-worker`. Both keys are
   readable now; new writes use the new key.
4. As root with the prod env loaded:
   `python scripts/rotate_encryption_key.py` (dry run; per-column counts),
   then `--apply` (single transaction), then dry run again. Accept: `old_key`
   is 0 everywhere and `undecryptable` equals the dry-run figure from before
   the change (pre-existing values nobody can read; investigate separately).
5. Remove `ENCRYPTION_MASTER_KEY_PREVIOUS`, restart both services, smoke:
   a Twilio-backed SMS send preview, an OAuth-connected integration page, and
   `/health`.
6. Rotate `SESSION_SECRET` too (it was exposed by the same unit). This signs
   everyone out; schedule it.

Rollback before step 5: put the old key back as primary; the rotated values are
still readable because the old key stays in `_PREVIOUS` until step 5. After
step 5, restore the step-1 backup only for the affected rows.

## Evidence

- Isolated drill (throwaway initdb cluster restored from an encrypted staging
  backup): dry run `provider_credential.encrypted_value old_key=2`, apply, dry
  run `current_key=2`; both values decrypt with the new key alone.
- `tests/test_secret_vault_rotation.py`: old values readable after rotation;
  production refuses missing/malformed keys; nested JSON values rotate; values
  under an unknown key are left untouched.
