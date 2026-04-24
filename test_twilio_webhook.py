"""
Local test script for /twilio/sms/inbound webhook.

Usage (Replit or local dev):
    python test_twilio_webhook.py

The script sends sample Twilio-style form-encoded POST payloads
directly to the running Flask app.  Twilio signature validation
is automatically bypassed when REPL_ID or REPLIT_DEV_DOMAIN is set.
For VPS testing, set SKIP_SIG_VALIDATION=1 or supply a real signed request.
"""

import os
import sys
import json
import time
import uuid
import requests

BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000")
WEBHOOK  = f"{BASE_URL}/twilio/sms/inbound"

TWILIO_NUMBER = os.environ.get("TWILIO_TEST_TO",   "+15550001234")
CALLER_NUMBER = os.environ.get("TWILIO_TEST_FROM", "+15559998888")

HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


def _sid():
    return "SM" + uuid.uuid4().hex


def post(label: str, payload: dict) -> None:
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"  Body: {payload.get('Body', '(empty)')!r}")
    try:
        r = requests.post(WEBHOOK, data=payload, headers=HEADERS, timeout=10)
        print(f"  HTTP {r.status_code}")
        body = r.text.strip()
        if body:
            print(f"  TwiML: {body[:300]}")
        else:
            print("  TwiML: (empty — no auto-reply)")
    except requests.exceptions.ConnectionError:
        print("  ERROR: Could not connect. Is the app running?")
    except Exception as exc:
        print(f"  ERROR: {exc}")


def make_payload(body: str, **extra) -> dict:
    return {
        "MessageSid":         _sid(),
        "From":               CALLER_NUMBER,
        "To":                 TWILIO_NUMBER,
        "Body":               body,
        "NumMedia":           "0",
        "MessagingServiceSid": "",
        **extra,
    }


if __name__ == "__main__":
    print(f"Testing Twilio SMS webhook at: {WEBHOOK}")
    print(f"  From: {CALLER_NUMBER}  →  To: {TWILIO_NUMBER}")

    # ── System keyword tests ───────────────────────────────────────────────
    post("STOP — should unsubscribe and reply",     make_payload("STOP"))
    time.sleep(0.3)
    post("START — should re-subscribe and reply",   make_payload("START"))
    time.sleep(0.3)
    post("HELP — should reply with support info",   make_payload("HELP"))
    time.sleep(0.3)

    # Case-insensitive keyword variants
    post("stop (lowercase) — same as STOP",         make_payload("stop"))
    time.sleep(0.3)
    post("help (mixed case)",                       make_payload("Help"))
    time.sleep(0.3)

    # ── Regular messages ───────────────────────────────────────────────────
    post("Generic inquiry — rule engine applies",
         make_payload("Hi, I'd like more information please."))
    time.sleep(0.3)

    post("Pricing keyword — should trigger pricing rule",
         make_payload("What is your pricing?"))
    time.sleep(0.3)

    post("After-hours check — may trigger after-hours rule",
         make_payload("Hey, is anyone there?"))
    time.sleep(0.3)

    # ── Media message ──────────────────────────────────────────────────────
    post("MMS with image",
         make_payload(
             "",
             NumMedia="1",
             MediaUrl0="https://example.com/image.jpg",
             MediaContentType0="image/jpeg",
         ))
    time.sleep(0.3)

    # ── Idempotency test ───────────────────────────────────────────────────
    dup_sid = _sid()
    post("First delivery of duplicate SID",
         make_payload("Duplicate message test", MessageSid=dup_sid))
    time.sleep(0.3)
    post("Second delivery of same SID — should be silently ignored",
         make_payload("Duplicate message test", MessageSid=dup_sid))

    print(f"\n{'='*60}")
    print("All tests done.")
    print()
    print("Expected results:")
    print("  STOP  → 200 with <Message>You have been unsubscribed…</Message>")
    print("  START → 200 with <Message>You have been subscribed…</Message>")
    print("  HELP  → 200 with <Message>LUXit SMS Support…</Message>")
    print("  Other → 200 with <Response></Response>  (replies sent via Twilio API)")
    print("  Dupe  → 200 with <Response></Response>  (silently skipped)")
