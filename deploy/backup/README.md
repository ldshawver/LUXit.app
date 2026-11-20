# LUXit database backups

Nothing scheduled backed up the production database before this (newest full
dump on 2026-10-19 was the 2026-09-08 pre-deploy dump, on the same disk).

- `luxit-db-backup.sh` - nightly pg_dump (custom format), TOC-verified, then
  gpg-encrypted to a public key; plaintext never kept. Writes
  `last-backup.status` for the health check. Refuses to run without a recipient.
- `luxit-backup-offsite.sh` - `OFFSITE_CMD`: rclone copy (`--immutable`) of the
  ciphertext + checksum, size-verified. Refuses non-`.gpg` files.
- `luxit-restore-drill.sh` - checksum, decrypt, restore into a throwaway
  `initdb` cluster (unix socket only, no TCP, not a pg_ctlcluster), sanity
  counts, destroy. Touches no existing database.

## Keys (owner action)

Generate the keypair off the server; only the public half goes on the host.

    gpg --quick-gen-key 'LUXit backups <backups@luxit.app>' rsa4096 encrypt never
    gpg --export backups@luxit.app > luxit-backup.pub          # copy to server
    gpg --export-secret-keys backups@luxit.app > OFFLINE-ONLY  # password manager + paper

    # on the server
    install -d -m 0700 /etc/luxit/backup-gnupg
    GNUPGHOME=/etc/luxit/backup-gnupg gpg --import luxit-backup.pub

## Install (production host change - needs explicit authorization)

    install -m 0750 luxit-db-backup.sh luxit-backup-offsite.sh luxit-restore-drill.sh /usr/local/sbin/
    install -m 0600 backup.env.example /etc/luxit/backup.env   # then edit
    install -m 0644 luxit-db-backup.service luxit-db-backup.timer /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now luxit-db-backup.timer
    systemctl start luxit-db-backup.service          # first backup now

Off-site (D-10): pick a bucket with object lock/versioning (B2, S3, R2), install
rclone, write `/etc/luxit/backup-rclone.conf` (0600), set `OFFSITE_REMOTE`.
Until then the health check reports `offsite=no` as failing, on purpose.

## Restore drill (monthly; bring the private key for the drill only)

    DRILL_GNUPGHOME=/root/drill-gnupg /usr/local/sbin/luxit-restore-drill.sh
    shred -u /root/drill-gnupg/private-keys-v1.d/*; rm -rf /root/drill-gnupg

Validated 2026-10-31 against the staging database: backup 0.9 MB / 2144 TOC
entries in 1 s; drill PASS (207 tables, 42 migrations, 15 companies) in 5 s;
tampered ciphertext, public-key-only drill and missing recipient all fail.
Restoring a real outage: same steps as the drill against the target cluster
with the app stopped, then `scripts/apply_migrations.py` to confirm the ledger.
