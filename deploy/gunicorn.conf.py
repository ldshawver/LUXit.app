"""Canonical Gunicorn worker model for the LUXit Flask app (all environments).

WHY THIS FILE EXISTS
--------------------
`/api/inbox/stream` (inbox_pwa.sse_stream) is a long-lived Server-Sent-Events
generator: it blocks in `while True: queue.get(timeout=25)` for the entire life
of the browser connection. Under the *sync* worker class each open PWA tab
therefore pins a whole worker process. Production ran `--workers 3` sync with no
threads, so 2-3 open PWA sessions starved the pool and the Twilio voice webhook
sat in the listen backlog past Twilio's ~15 s deadline -> ErrorCode 11200 /
nginx 499 / no <Dial> returned (prod incident 2026-09-08).

The fix is the **gthread** worker class: an SSE client then holds one *thread*,
not a whole worker, exactly as `sse_stream`'s own docstring already requires.

USAGE
-----
    gunicorn -c deploy/gunicorn.conf.py app:app          # staging / prod
    gunicorn -c deploy/gunicorn.conf.py dev_wsgi:app     # dev (via --pythonpath)

Every value can be overridden from the environment so one file serves dev,
staging and prod:

    GUNICORN_BIND          (default 127.0.0.1:8001)
    GUNICORN_WORKERS       (default 4)
    GUNICORN_THREADS       (default 8)
    GUNICORN_TIMEOUT       (default 60)

The live systemd units currently pass these as explicit ExecStart flags; keep
this file and the units in agreement. See deploy/WORKER_MODEL.md.
"""
import os

bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8001")
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))

# gthread: long-lived SSE connections consume a thread, not a worker, so the
# Twilio voice webhook is never blocked behind an open PWA inbox stream.
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "8"))

# Worker-liveness timeout. gthread workers heartbeat the arbiter from a side
# thread, so a streaming response does not trip this; 60 s is well under
# Twilio's ~15 s webhook deadline for *request* handling and keeps a genuinely
# wedged worker from lingering the way the old --timeout 300 did.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 30
keepalive = 5

# Recycle workers periodically to bound any slow leak.
max_requests = 2000
max_requests_jitter = 200

errorlog = "-"
accesslog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
capture_output = True
enable_stdio_inheritance = True
