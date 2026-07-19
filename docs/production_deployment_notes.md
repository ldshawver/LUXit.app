# LUXit production deployment notes

- `lux-email-bot.service` binding `127.0.0.1:8001` is the canonical public LUXit.app service. Nginx for `luxit.app` and `app.luxit.app` must proxy to this port.
- `luxit.service` binding `127.0.0.1:8000` is a legacy duplicate. Do not point production traffic to port 8000 as a recovery shortcut.
- Only one scheduler-enabled application instance should run in production. After `lux-email-bot.service` is healthy on port 8001 and public `/healthz` passes, operators should stop the duplicate legacy service using the normal change-control process.
- After this incident's exposed live session/remember cookie, perform a controlled rotation of the Flask `SECRET_KEY`/session-signing secret after deploying the fix. This invalidates existing sessions and remember cookies. Do not rotate production secrets automatically from CI.
