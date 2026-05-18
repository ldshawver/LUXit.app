#!/usr/bin/env python3
"""
VPS one-time setup: configure SMS forwarding for +19165989519 → +12792860000.

Run this on the VPS:
  python3 scripts/configure_sms_forwarding.py

What it does:
  1. Finds (or creates) the TwilioAccount row for the luxit.app company
  2. Sets sms_forward_to = +12792860000
  3. Sets sms_forwarding_enabled = True
  4. Sets from_phone = +19165989519 (the Twilio number)
  5. Sets webhook_base_url = https://luxit.app
  6. Confirms Twilio webhook URLs are correctly set on the number
  7. Sends a final verification SMS to confirm forwarding is live
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

TWILIO_NUMBER  = "+19165989519"
FORWARD_TO     = "+12792860000"
WEBHOOK_BASE   = "https://luxit.app"
SMS_WEBHOOK    = f"{WEBHOOK_BASE}/twilio/sms/inbound"
VOICE_WEBHOOK  = f"{WEBHOOK_BASE}/twilio/voice/inbound"


def main():
    print("=" * 60)
    print("LUXit SMS Forwarding Setup")
    print("=" * 60)

    # ── 1. Boot Flask app ─────────────────────────────────────
    from app import create_app
    app = create_app()

    with app.app_context():
        from extensions import db
        from models import TwilioAccount, Company

        # ── 2. Find company ───────────────────────────────────
        company = Company.query.order_by(Company.id).first()
        if not company:
            print("ERROR: No company found in database. Run the app first to seed data.")
            sys.exit(1)
        print(f"✓ Using company: {company.name} (id={company.id})")

        # ── 3. Find or create TwilioAccount ──────────────────
        ta = TwilioAccount.query.filter_by(company_id=company.id).first()
        if not ta:
            print("  Creating new TwilioAccount row...")
            ta = TwilioAccount(company_id=company.id)
            db.session.add(ta)
        else:
            print(f"  Found existing TwilioAccount id={ta.id}")

        # ── 4. Set credentials from env ───────────────────────
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        tok = os.environ.get("TWILIO_AUTH_TOKEN", "")
        if sid and not ta.get_account_sid():
            ta.set_account_sid(sid)
            print("  ✓ Account SID stored (encrypted)")
        if tok and not ta.get_auth_token():
            ta.set_auth_token(tok)
            print("  ✓ Auth token stored (encrypted)")

        # ── 5. Configure forwarding ───────────────────────────
        ta.from_phone             = TWILIO_NUMBER
        ta.sms_forward_to         = FORWARD_TO
        ta.sms_forwarding_enabled = True
        ta.call_forward_to        = FORWARD_TO
        ta.voice_forwarding_enabled = True
        ta.webhook_base_url       = WEBHOOK_BASE
        ta.is_active              = True
        ta.automation_enabled     = True

        db.session.commit()
        print(f"\n✓ Forwarding configured:")
        print(f"  Twilio number : {TWILIO_NUMBER}")
        print(f"  Forward SMS to: {FORWARD_TO}")
        print(f"  Forward calls : {FORWARD_TO}")
        print(f"  Webhook base  : {WEBHOOK_BASE}")

    # ── 6. Verify Twilio webhook URLs on the number ──────────
    print("\nChecking Twilio webhook URLs...")
    try:
        from twilio.rest import Client
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        tok = os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not (sid and tok):
            print("  WARN: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set — skipping webhook check")
        else:
            client = Client(sid, tok)
            numbers = client.incoming_phone_numbers.list(phone_number=TWILIO_NUMBER)
            if not numbers:
                print(f"  ERROR: {TWILIO_NUMBER} not found in Twilio account")
            else:
                n = numbers[0]
                sms_ok   = n.sms_url == SMS_WEBHOOK
                voice_ok = n.voice_url == VOICE_WEBHOOK
                print(f"  SMS webhook  : {n.sms_url}  {'✓' if sms_ok else '⚠ WRONG'}")
                print(f"  Voice webhook: {n.voice_url}  {'✓' if voice_ok else '⚠ WRONG'}")

                # Auto-fix if wrong
                needs_update = {}
                if not sms_ok:
                    needs_update["sms_url"]    = SMS_WEBHOOK
                    needs_update["sms_method"] = "POST"
                if not voice_ok:
                    needs_update["voice_url"]    = VOICE_WEBHOOK
                    needs_update["voice_method"] = "POST"
                if needs_update:
                    print("  Updating webhook URLs on Twilio...")
                    n.update(**needs_update)
                    print("  ✓ Webhook URLs updated")

    except Exception as exc:
        print(f"  Webhook check error: {exc}")

    # ── 7. Send verification SMS ──────────────────────────────
    print("\nSending verification SMS to confirm forwarding...")
    try:
        from twilio.rest import Client
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        tok = os.environ.get("TWILIO_AUTH_TOKEN", "")
        if sid and tok:
            client = Client(sid, tok)
            msg = client.messages.create(
                body=(
                    "[LUXit] SMS forwarding is LIVE. "
                    f"Texts to {TWILIO_NUMBER} will arrive on this phone. "
                    "Reply 'reply +1XXXXXXXXXX <msg>' to respond to a customer."
                ),
                from_=TWILIO_NUMBER,
                to=FORWARD_TO,
            )
            print(f"  ✓ Verification SMS sent: SID={msg.sid} status={msg.status}")
    except Exception as exc:
        print(f"  Verification SMS error: {exc}")

    print("\n" + "=" * 60)
    print("Setup complete. Test by texting ANY message to:")
    print(f"  {TWILIO_NUMBER}")
    print(f"It will be forwarded to {FORWARD_TO}")
    print()
    print("To reply to a customer from your phone, text:")
    print("  reply +1XXXXXXXXXX Your message here")
    print("  r Your message here  (replies to most recent customer)")
    print("=" * 60)


if __name__ == "__main__":
    main()
