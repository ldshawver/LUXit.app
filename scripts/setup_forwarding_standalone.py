#!/usr/bin/env python3
"""
Standalone SMS forwarding setup — run from ANYWHERE on the VPS.
No Flask, no app directory needed.

Usage:
    python3 setup_forwarding_standalone.py

Reads credentials from environment variables (already set on your VPS):
    DATABASE_URL, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
    DATA_ENCRYPTION_KEY  (for encrypting the stored token)
"""
import os
import sys
import json

TWILIO_NUMBER = "+19165989519"
FORWARD_TO    = "+12792860000"
WEBHOOK_BASE  = "https://luxit.app"

def fail(msg):
    print(f"\n✗ ERROR: {msg}")
    sys.exit(1)


# ── 0. Collect credentials ────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
ENC_KEY      = os.environ.get("DATA_ENCRYPTION_KEY", "")

if not DATABASE_URL:
    fail("DATABASE_URL not set. Export it first:\n  export DATABASE_URL=postgresql://...")
if not TWILIO_SID or not TWILIO_TOKEN:
    fail("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set.")

print("=" * 62)
print("  LUXit SMS Forwarding Setup")
print("=" * 62)

# ── 1. Encrypt the auth token (Fernet) ───────────────────────────────────────
encrypted_sid   = None
encrypted_token = None
if ENC_KEY:
    try:
        from cryptography.fernet import Fernet
        f = Fernet(ENC_KEY.encode() if isinstance(ENC_KEY, str) else ENC_KEY)
        encrypted_sid   = f.encrypt(TWILIO_SID.encode()).decode()
        encrypted_token = f.encrypt(TWILIO_TOKEN.encode()).decode()
        print("  ✓ Credentials will be stored encrypted (Fernet)")
    except Exception as exc:
        print(f"  ⚠  Encryption skipped ({exc}) — storing plaintext")
        encrypted_sid   = TWILIO_SID
        encrypted_token = TWILIO_TOKEN
else:
    print("  ⚠  DATA_ENCRYPTION_KEY not set — storing plaintext credentials")
    encrypted_sid   = TWILIO_SID
    encrypted_token = TWILIO_TOKEN

# ── 2. Connect to Postgres ────────────────────────────────────────────────────
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    fail("psycopg2 not installed.\n  Run: pip install psycopg2-binary")

try:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    print("  ✓ Connected to Postgres")
except Exception as exc:
    fail(f"Cannot connect to database: {exc}")

# ── 3. Find the first company ─────────────────────────────────────────────────
cur.execute("SELECT id, name FROM company ORDER BY id LIMIT 1;")
company = cur.fetchone()
if not company:
    fail("No company rows found — boot the app once so it seeds the DB.")
company_id   = company["id"]
company_name = company["name"]
print(f"  ✓ Company: {company_name} (id={company_id})")

# ── 4. Upsert TwilioAccount ───────────────────────────────────────────────────
cur.execute(
    "SELECT id, sms_forward_to, sms_forwarding_enabled FROM twilio_account WHERE company_id = %s;",
    (company_id,)
)
row = cur.fetchone()

if row:
    print(f"  ✓ TwilioAccount exists (id={row['id']}) — updating forwarding settings")
    cur.execute(
        """
        UPDATE twilio_account SET
            from_phone              = %s,
            sms_forward_to          = %s,
            sms_forwarding_enabled  = TRUE,
            call_forward_to         = %s,
            voice_forwarding_enabled = TRUE,
            webhook_base_url        = %s,
            is_active               = TRUE,
            account_sid             = %s,
            auth_token              = %s
        WHERE company_id = %s;
        """,
        (TWILIO_NUMBER, FORWARD_TO, FORWARD_TO, WEBHOOK_BASE,
         encrypted_sid, encrypted_token, company_id)
    )
else:
    print("  Creating new TwilioAccount row...")
    cur.execute(
        """
        INSERT INTO twilio_account
            (company_id, from_phone, sms_forward_to, sms_forwarding_enabled,
             call_forward_to, voice_forwarding_enabled,
             webhook_base_url, is_active, automation_enabled,
             account_sid, auth_token,
             missed_call_text, after_hours_text)
        VALUES (%s, %s, %s, TRUE, %s, TRUE, %s, TRUE, TRUE, %s, %s,
                'Sorry we missed your call! Reply to schedule a callback.',
                'Thanks for reaching out! Our team is currently away. We''ll reply during business hours.');
        """,
        (company_id, TWILIO_NUMBER, FORWARD_TO, FORWARD_TO, WEBHOOK_BASE,
         encrypted_sid, encrypted_token)
    )

conn.commit()
cur.close()
conn.close()
print(f"\n  ✓ Forwarding configured:")
print(f"    Twilio number  : {TWILIO_NUMBER}")
print(f"    Forward SMS to : {FORWARD_TO}")
print(f"    Forward calls  : {FORWARD_TO}")
print(f"    Webhook base   : {WEBHOOK_BASE}")

# ── 5. Verify + fix Twilio webhook URLs ──────────────────────────────────────
print("\nChecking Twilio webhook URLs...")
SMS_WEBHOOK   = f"{WEBHOOK_BASE}/twilio/sms/inbound"
VOICE_WEBHOOK = f"{WEBHOOK_BASE}/twilio/voice/inbound"
try:
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    numbers = client.incoming_phone_numbers.list(phone_number=TWILIO_NUMBER)
    if not numbers:
        print(f"  ⚠  {TWILIO_NUMBER} not found in Twilio account")
    else:
        n = numbers[0]
        sms_ok   = n.sms_url == SMS_WEBHOOK
        voice_ok = n.voice_url == VOICE_WEBHOOK
        print(f"  SMS webhook  : {n.sms_url}  {'✓' if sms_ok else '← WRONG'}")
        print(f"  Voice webhook: {n.voice_url}  {'✓' if voice_ok else '← WRONG'}")

        fix = {}
        if not sms_ok:
            fix["sms_url"]    = SMS_WEBHOOK
            fix["sms_method"] = "POST"
        if not voice_ok:
            fix["voice_url"]    = VOICE_WEBHOOK
            fix["voice_method"] = "POST"
        if fix:
            n.update(**fix)
            print("  ✓ Webhook URLs corrected on Twilio")
except ImportError:
    print("  ⚠  twilio package not installed — skipping webhook check")
    print("     Run: pip install twilio")
except Exception as exc:
    print(f"  ⚠  Webhook check error: {exc}")

# ── 6. Send confirmation SMS ─────────────────────────────────────────────────
print("\nSending confirmation SMS to your SIM...")
try:
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    msg = client.messages.create(
        body=(
            f"[LUXit] Forwarding is LIVE. "
            f"Texts to {TWILIO_NUMBER} will arrive here. "
            "Reply format: 'reply +1XXXXXXXXXX <msg>' or 'r <msg>' for most recent."
        ),
        from_=TWILIO_NUMBER,
        to=FORWARD_TO,
    )
    print(f"  ✓ Confirmation SMS sent (SID={msg.sid}, status={msg.status})")
except Exception as exc:
    print(f"  ⚠  Could not send confirmation SMS: {exc}")

print("\n" + "=" * 62)
print("  DONE. Forward is live.")
print()
print("  Test it: text anything to")
print(f"    {TWILIO_NUMBER}")
print(f"  It will arrive on {FORWARD_TO}")
print()
print("  To reply from your SIM:")
print("    reply +1XXXXXXXXXX  Your reply here")
print("    r  Your reply here        (most recent customer)")
print("=" * 62)
