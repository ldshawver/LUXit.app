#!/usr/bin/env python3
"""P1 acceptance probe — the Twilio voice webhook must stay fast while PWA SSE
streams are open.

Runs against a LIVE gunicorn service (dev :8002 / staging :8004) configured with
the gthread worker model. It:

  1. seeds an idempotent tenant fixture directly in that env's Postgres
     (company + twilio_account + voice number + one eligible/available user),
  2. forges a Flask-Login session cookie for that user with the env SESSION_SECRET
     (no password needed, no mutation of auth data),
  3. holds N concurrent GET /api/inbox/stream (SSE) connections open,
  4. while they are open, times:
        POST /twilio/voice/inbound   (properly X-Twilio-Signature signed)
        GET  /api/phone/voice-token
  5. asserts every measured latency is well under Twilio's ~15 s webhook
     deadline (default gate: 2.0 s) and that the webhook returns ring_pwa TwiML.

Exit 0 = PASS, 1 = FAIL. Prints a machine-readable RESULT: line.

Usage:
    python scripts/acceptance/voice_sse_concurrency.py \
        --base http://127.0.0.1:8004 \
        --db  "postgresql://luxstaging:...@127.0.0.1:5434/luxstaging" \
        --session-secret "$SESSION_SECRET" \
        [--sse 4] [--iterations 8] [--gate 2.0] [--keep-fixture]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import sys
import threading
import time

import psycopg2
import requests
from itsdangerous import URLSafeTimedSerializer
from twilio.request_validator import RequestValidator

try:
    from flask.json.tag import TaggedJSONSerializer
except Exception:  # older/newer flask
    from flask.sessions import TaggedJSONSerializer  # type: ignore

MARK = "sse-accept"
NUMBER = "+18305550199"
CALLER = "+14155550142"
AUTH_TOKEN = "sse-accept-voice-webhook-token"  # plaintext; get_auth_token() falls back to raw


# ─────────────────────────── fixture ────────────────────────────
def seed_fixture(db_url: str) -> dict:
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM company WHERE name = %s", (f"{MARK}-tenant",))
        row = cur.fetchone()
        if row:
            company_id = row[0]
            cur.execute("UPDATE company SET require_approved_pwa_devices = FALSE, is_active = TRUE WHERE id = %s", (company_id,))
        else:
            cur.execute(
                "INSERT INTO company (name, is_active, require_approved_pwa_devices, created_at, updated_at) "
                "VALUES (%s, TRUE, FALSE, now(), now()) RETURNING id", (f"{MARK}-tenant",))
            company_id = cur.fetchone()[0]

        cur.execute("SELECT id FROM twilio_account WHERE company_id = %s", (company_id,))
        row = cur.fetchone()
        if row:
            account_id = row[0]
            cur.execute(
                "UPDATE twilio_account SET from_phone=%s, is_active=TRUE, auth_token=%s, "
                "webhook_base_url=%s, after_hours_voicemail_enabled=TRUE WHERE id=%s",
                (NUMBER, AUTH_TOKEN, "__BASE__", account_id))
        else:
            cur.execute(
                "INSERT INTO twilio_account (company_id, from_phone, is_active, auth_token, webhook_base_url, "
                "after_hours_voicemail_enabled, created_at, updated_at) "
                "VALUES (%s,%s,TRUE,%s,%s,TRUE, now(), now()) RETURNING id",
                (company_id, NUMBER, AUTH_TOKEN, "__BASE__"))
            account_id = cur.fetchone()[0]

        cur.execute("SELECT id FROM twilio_phone_number WHERE phone_number = %s", (NUMBER,))
        row = cur.fetchone()
        if row:
            pn_id = row[0]
            cur.execute(
                "UPDATE twilio_phone_number SET company_id=%s, twilio_account_id=%s, is_active=TRUE, "
                "voice_enabled=TRUE, during_hours_route='ring_pwa', after_hours_route='ring_pwa', "
                "after_hours_voicemail_enabled=TRUE WHERE id=%s", (company_id, account_id, pn_id))
        else:
            cur.execute(
                "INSERT INTO twilio_phone_number (company_id, twilio_account_id, phone_number, friendly_name, "
                "voice_enabled, is_active, during_hours_route, after_hours_route, after_hours_voicemail_enabled, "
                "created_at, updated_at) VALUES (%s,%s,%s,%s,TRUE,TRUE,'ring_pwa','ring_pwa',TRUE, now(), now()) RETURNING id",
                (company_id, account_id, NUMBER, f"{MARK} line"))
            pn_id = cur.fetchone()[0]

        email = f"{MARK}-owner@example.invalid"
        cur.execute('SELECT id FROM "user" WHERE email = %s', (email,))
        row = cur.fetchone()
        if row:
            user_id = row[0]
            cur.execute('UPDATE "user" SET active = TRUE WHERE id = %s', (user_id,))
        else:
            cur.execute(
                'INSERT INTO "user" (username, email, active, created_at, updated_at) '
                "VALUES (%s,%s,TRUE, now(), now()) RETURNING id", (f"{MARK}-owner", email))
            user_id = cur.fetchone()[0]

        cur.execute("SELECT id FROM user_company_access WHERE user_id=%s AND company_id=%s", (user_id, company_id))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE user_company_access SET role='owner', is_active=TRUE, receive_calls=TRUE, "
                "phone_availability='available' WHERE id=%s", (row[0],))
        else:
            cur.execute(
                "INSERT INTO user_company_access (user_id, company_id, role, is_active, receive_calls, "
                "phone_availability, created_at, updated_at) "
                "VALUES (%s,%s,'owner',TRUE,TRUE,'available', now(), now())", (user_id, company_id))

        # Phone/PWA Communications license so /api/inbox/stream is not 402-gated.
        cur.execute("SELECT id FROM tenant_license WHERE company_id=%s AND feature_key=%s",
                    (company_id, "phone_pwa_communications"))
        if cur.fetchone():
            cur.execute("UPDATE tenant_license SET status='active' "
                        "WHERE company_id=%s AND feature_key=%s", (company_id, "phone_pwa_communications"))
        else:
            cur.execute(
                "INSERT INTO tenant_license (company_id, feature_key, status, seats_included, seats_used, "
                "monthly_price, billing_cycle, starts_at, auto_disable_enabled, grace_period_days, created_at, updated_at) "
                "VALUES (%s,'phone_pwa_communications','active',999,0,0,'monthly', now(), TRUE, 7, now(), now())",
                (company_id,))
        conn.commit()
        return {"company_id": company_id, "account_id": account_id, "pn_id": pn_id, "user_id": user_id}
    finally:
        cur.close(); conn.close()


def set_webhook_base(db_url: str, base: str) -> None:
    conn = psycopg2.connect(db_url); conn.autocommit = True
    conn.cursor().execute("UPDATE twilio_account SET webhook_base_url = %s WHERE from_phone = %s", (base, NUMBER))
    conn.close()


def drop_fixture(db_url: str) -> None:
    """Best-effort teardown. The fixture is idempotent, so a partial cleanup
    (rows still referenced by notification/call_event/etc.) is harmless."""
    conn = psycopg2.connect(db_url); conn.autocommit = True
    email = f"{MARK}-owner@example.invalid"
    stmts = [
        ('SELECT id FROM company WHERE name=%s', (f"{MARK}-tenant",)),
    ]
    cur = conn.cursor()
    cur.execute(*stmts[0])
    row = cur.fetchone()
    company_id = row[0] if row else None
    for sql, params in [
        ('DELETE FROM notification WHERE user_id IN (SELECT id FROM "user" WHERE email=%s)', (email,)),
        ("DELETE FROM twilio_call_log WHERE to_number=%s", (NUMBER,)),
        ("DELETE FROM call_event WHERE call_log_id NOT IN (SELECT id FROM twilio_call_log)", ()),
        ("DELETE FROM tenant_license WHERE company_id=%s", (company_id,)),
        ("DELETE FROM phone_settings WHERE company_id=%s", (company_id,)),
        ('DELETE FROM user_company_access WHERE user_id IN (SELECT id FROM "user" WHERE email=%s)', (email,)),
        ('DELETE FROM "user" WHERE email=%s', (email,)),
        ("DELETE FROM twilio_phone_number WHERE phone_number=%s", (NUMBER,)),
        ("DELETE FROM twilio_account WHERE from_phone=%s", (NUMBER,)),
        ("DELETE FROM company WHERE name=%s", (f"{MARK}-tenant",)),
    ]:
        try:
            cur.execute(sql, params)
        except Exception as e:  # noqa: BLE001
            print(f"  (cleanup skip: {str(e).splitlines()[0]})")
    conn.close()


# ─────────────────────────── auth cookie ────────────────────────────
def forge_session_cookie(secret: str, user_id: int) -> str:
    s = URLSafeTimedSerializer(
        secret, salt="cookie-session", serializer=TaggedJSONSerializer(),
        signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1},
    )
    return s.dumps({"_user_id": str(user_id), "_fresh": False})


# ─────────────────────────── probe ────────────────────────────
class SSEHolder(threading.Thread):
    def __init__(self, base, cookies):
        super().__init__(daemon=True)
        self.base, self.cookies = base, cookies
        self.connected = threading.Event()
        self.error = None
        self._stop = threading.Event()

    def run(self):
        try:
            r = requests.get(f"{self.base}/api/inbox/stream", cookies=self.cookies,
                             stream=True, timeout=(5, 90))
            if r.status_code != 200:
                self.error = f"SSE HTTP {r.status_code}"
                return
            for _line in r.iter_lines():
                self.connected.set()
                if self._stop.is_set():
                    r.close(); return
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"

    def stop(self):
        self._stop.set()


def signed_webhook_headers(base, params):
    sig = RequestValidator(AUTH_TOKEN).compute_signature(f"{base}/twilio/voice/inbound", params)
    return {"X-Twilio-Signature": sig}


def time_call(fn):
    t0 = time.perf_counter()
    resp = fn()
    return (time.perf_counter() - t0), resp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--session-secret", required=True)
    ap.add_argument("--sse", type=int, default=4)
    ap.add_argument("--iterations", type=int, default=8)
    ap.add_argument("--gate", type=float, default=2.0)
    ap.add_argument("--keep-fixture", action="store_true")
    args = ap.parse_args()

    ids = seed_fixture(args.db)
    set_webhook_base(args.db, args.base)
    cookies = {"session": forge_session_cookie(args.session_secret, ids["user_id"])}

    # sanity: cookie authenticates
    tok0 = requests.get(f"{args.base}/api/phone/voice-token", cookies=cookies, timeout=10)
    auth_ok = tok0.status_code in (200, 403, 503)  # anything but 401 = the forged cookie authenticated
    if tok0.status_code == 401:
        print("RESULT: FAIL reason=forged_session_cookie_not_accepted")
        if not args.keep_fixture:
            drop_fixture(args.db)
        return 1

    def do_webhook(sid):
        params = {"To": NUMBER, "From": CALLER, "CallSid": sid, "Direction": "inbound", "CallStatus": "ringing"}
        return requests.post(f"{args.base}/twilio/voice/inbound", data=params,
                             headers=signed_webhook_headers(args.base, params), timeout=20)

    def do_token():
        return requests.get(f"{args.base}/api/phone/voice-token", cookies=cookies, timeout=20)

    # baseline (no SSE)
    base_wh = [time_call(lambda: do_webhook(f"BASE{i}")) for i in range(3)]
    base_tk = [time_call(do_token) for _ in range(3)]

    # open SSE streams
    holders = [SSEHolder(args.base, cookies) for _ in range(args.sse)]
    for h in holders:
        h.start()
    deadline = time.time() + 20
    for h in holders:
        while not h.connected.is_set() and h.error is None and time.time() < deadline:
            time.sleep(0.1)
    live = sum(1 for h in holders if h.connected.is_set())
    sse_errors = [h.error for h in holders if h.error]
    if live < args.sse:
        print(f"RESULT: FAIL reason=only_{live}_of_{args.sse}_sse_connected errors={sse_errors}")
        for h in holders:
            h.stop()
        if not args.keep_fixture:
            drop_fixture(args.db)
        return 1

    time.sleep(1.0)  # let streams settle into the heartbeat loop

    # under load — sequential
    seq_wh = [time_call(lambda: do_webhook(f"SEQ{i}")) for i in range(args.iterations)]
    seq_tk = [time_call(do_token) for _ in range(args.iterations)]

    # under load — concurrent burst
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = []
        for i in range(args.iterations):
            futs.append(ex.submit(time_call, lambda i=i: do_webhook(f"CONC{i}")))
            futs.append(ex.submit(time_call, do_token))
        burst = [f.result() for f in futs]

    for h in holders:
        h.stop()

    # analyse
    def lat(pairs):
        return sorted(round(p[0], 3) for p in pairs)

    def status_set(pairs):
        return sorted({p[1].status_code for p in pairs})

    wh_pairs = seq_wh + [b for b in burst if b[1].request.method == "POST"]
    tk_pairs = seq_tk + [b for b in burst if b[1].request.method == "GET"]
    wh_lat, tk_lat = lat(wh_pairs), lat(tk_pairs)
    wh_max, tk_max = max(wh_lat), max(tk_lat)

    # routing correctness on the signed webhook
    sample = seq_wh[0][1]
    body = sample.text
    ring_pwa_ok = sample.status_code == 200 and "<Dial" in body and "<Client>" in body and "<Identity>" in body

    report = {
        "base": args.base,
        "sse_connections_live": live,
        "baseline_webhook_lat_s": lat(base_wh),
        "baseline_token_lat_s": lat(base_tk),
        "under_load_webhook_lat_s": wh_lat,
        "under_load_token_lat_s": tk_lat,
        "webhook_max_s": wh_max,
        "token_max_s": tk_max,
        "webhook_status_codes": status_set(wh_pairs),
        "token_status_codes": status_set(tk_pairs),
        "webhook_ring_pwa_twiml": ring_pwa_ok,
        "gate_s": args.gate,
    }
    print("REPORT:", json.dumps(report, indent=2))

    # The probe proves the *worker model*: every request answers well within
    # Twilio's ~15 s webhook deadline while N SSE streams are held open.
    #   webhook -> must be a fast 200 carrying ring_pwa TwiML (also proves P2)
    #   token   -> must be fast; 200 (creds present, e.g. staging), 403 (a real
    #              authz decision) and 503 (creds not configured, e.g. dev) are
    #              all valid *fast* answers -- a starved pool shows up as latency.
    ok = (wh_max < args.gate and tk_max < args.gate
          and status_set(wh_pairs) == [200]
          and all(s in (200, 403, 503) for s in status_set(tk_pairs))
          and ring_pwa_ok and auth_ok)

    if not args.keep_fixture:
        drop_fixture(args.db)

    print(f"RESULT: {'PASS' if ok else 'FAIL'} "
          f"webhook_max={wh_max:.3f}s token_max={tk_max:.3f}s sse={live} ring_pwa_twiml={ring_pwa_ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
