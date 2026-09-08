# Gunicorn worker model — canonical reference

The authoritative place the running command is defined is the **systemd unit
`ExecStart`** for each environment (there is no repo-managed process manager).
`deploy/gunicorn.conf.py` mirrors the same values and is the reviewable source
of intent; keep the two in agreement.

## Required model (all environments)

| Setting        | Value    | Why |
|----------------|----------|-----|
| `--worker-class`| `gthread`| `/api/inbox/stream` is a blocking long-lived SSE generator. Under `sync` each open PWA tab pins a whole worker; the Twilio voice webhook then times out in the backlog (prod incident 2026-09-08: 11200 / nginx 499 / no `<Dial>`). With `gthread` an SSE client holds one *thread*. |
| `--workers`     | `4`      | CPU-bound headroom. |
| `--threads`     | `8`      | 4×8 = 32 concurrent handlers; SSE fan-out lives here. |
| `--timeout`     | `60`     | Worker-liveness only under gthread; replaces the old `--timeout 300` that let a wedged worker linger 5 min. |

## Live units

### dev — `/etc/systemd/system/lux-email-bot-dev.service`
```
ExecStart=/srv/lux-email-bot-dev/.venv/bin/gunicorn --pythonpath /etc/luxit-dev \
  --workers 4 --worker-class gthread --threads 8 --bind 127.0.0.1:8002 --timeout 60 \
  --access-logfile - --error-logfile - dev_wsgi:app
```

### staging — `/etc/systemd/system/lux-email-bot-staging.service`
```
ExecStart=/srv/lux-email-bot-staging/.venv/bin/gunicorn \
  --workers 4 --worker-class gthread --threads 8 --bind 127.0.0.1:8004 --timeout 60 \
  --access-logfile - --error-logfile - app:app
```

### production — NOT YET APPLIED (change frozen pending authorization)

Production's gunicorn command comes from the base unit
`/etc/systemd/system/lux-email-bot.service` plus the approved drop-in
`/etc/systemd/system/lux-email-bot.service.d/recovery-20260824-candidate-54af654.conf`
(which already pins `WorkingDirectory=/root/luxit-main-canonicalize`).

Apply by editing **that existing drop-in** (do not add a new one — avoids a
conflicting duplicate `ExecStart`). The drop-in must clear the inherited
`ExecStart` before redefining it:

```ini
[Service]
WorkingDirectory=/root/luxit-main-canonicalize
Environment=GIT_SHA=<deployed sha>
Environment=APP_VERSION=<deployed sha short>
ExecStart=
ExecStart=/root/lux-email-bot/.venv/bin/gunicorn \
    --workers 4 --worker-class gthread --threads 8 \
    --bind 127.0.0.1:8001 --timeout 60 \
    --log-level info --capture-output --error-logfile - \
    app:app
```

Then `systemctl daemon-reload && systemctl restart lux-email-bot.service` and
confirm `/health` 200 + `ps -o args -p $(pgrep -f 'gunicorn.*8001' | head -1)`
shows `--worker-class gthread`.
