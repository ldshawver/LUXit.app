# LUXit database backups

Nothing scheduled backed up the production database before this (newest full
dump on 2026-10-19 was the 2026-09-08 pre-deploy dump, on the same disk).

Install (production host change — needs explicit authorization):

    install -m 0750 luxit-db-backup.sh /usr/local/sbin/luxit-db-backup.sh
    install -m 0750 luxit-restore-drill.sh /usr/local/sbin/luxit-restore-drill.sh
    install -m 0644 luxit-db-backup.service luxit-db-backup.timer /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now luxit-db-backup.timer
    systemctl start luxit-db-backup.service          # first backup now
    /usr/local/sbin/luxit-restore-drill.sh            # prove it restores

Still open: an off-site destination (S3/B2/another host). Set `OFFSITE_CMD`
in a drop-in once credentials exist. Run the restore drill monthly.
