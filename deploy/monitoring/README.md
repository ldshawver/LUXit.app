# LUXit health check and alerting

`luxit_healthcheck.py` (stdlib only, read-only) every 5 minutes via
`luxit-healthcheck.timer`. Checks: local + public `/health` (status, db),
systemd units (prod web, worker, postgres), nginx 5xx rate over 10 min
(SSE streams excluded), backup freshness/result/off-site, disk, MemAvailable
(host has no swap), TLS expiry. Each failure carries the next action.
Alerts on change and every `REPEAT_MINUTES` while failing; recoveries announced.

Channel (D-10): `ALERT_WEBHOOK_URL` (Slack/Teams/generic JSON) and/or
`ALERT_EMAIL_TO` (needs sendmail; none installed on the host today). With
neither, failures go to the journal and the unit exits non-zero.

Install (host change - needs approval):

    install -m 0750 luxit_healthcheck.py /usr/local/sbin/
    install -m 0600 healthcheck.env.example /etc/luxit/healthcheck.env  # set a channel
    install -m 0644 luxit-healthcheck.service luxit-healthcheck.timer /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now luxit-healthcheck.timer

Try it without alerting: `python3 luxit_healthcheck.py --print --no-alert`.
Prove alerting: point `HEALTH_URLS` at a closed port once and watch the channel.
External uptime (a monitor outside this VPS) is still needed: this check cannot
report a dead host.
