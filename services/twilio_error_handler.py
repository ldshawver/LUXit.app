"""
Shared Twilio error → human-readable message converter.

Import and call ``twilio_friendly_error(exc)`` anywhere a TwilioRestException
(or any other exception from the Twilio SDK) needs to be surfaced to the user.
Returns a single-line, action-oriented string — safe to show in a toast, flash
message, or JSON error field.
"""

def twilio_friendly_error(exc) -> str:
    """Return a user-friendly explanation for any Twilio send/call failure."""
    raw    = str(exc) or type(exc).__name__
    code   = getattr(exc, "code",   None)
    status = getattr(exc, "status", None)

    guidance = {
        # ── SMS ───────────────────────────────────────────────────────────
        21211: "The recipient phone number is invalid. Check the number and try again.",
        21408: "Twilio is not allowed to send to that destination. Enable the region in Twilio Console → Voice & Messaging → Geo Permissions.",
        21606: "The configured Twilio From number cannot send SMS. Check SMS Settings or use an SMS-capable number.",
        21610: "This customer has opted out. They must reply START before texts can be sent again.",
        21612: "Twilio cannot route SMS to that number. Check the recipient number and carrier support.",
        21614: "The recipient does not appear to be a mobile/SMS-capable number.",
        21617: "The message is too long for Twilio to send. Shorten it and try again.",
        30003: "The carrier could not deliver the message. Verify the customer's number or try calling.",
        30004: "The destination handset could not receive the text. Try again later or call the customer.",
        30005: "The destination number is unknown or inactive. Check the customer's phone number.",
        30006: "The destination number is a landline or unreachable by SMS. Try calling instead.",
        30007: "The carrier filtered the message. Reword it to avoid links or promotional wording, then try again.",
        # ── Voice / calls ─────────────────────────────────────────────────
        20003: "Twilio authentication failed. Check the Account SID and Auth Token in SMS Settings.",
        13224: "Invalid phone number format. Make sure the number is in E.164 format (+1XXXXXXXXXX).",
        13227: "Twilio geographic permissions block calls to that destination. Enable the region in Twilio Console → Voice & Messaging → Geo Permissions.",
        21201: "International calling is not enabled. Enable it in Twilio Console → Voice & Messaging → Geo Permissions.",
        21210: "The From number is not a valid Twilio phone number. Check SMS Settings.",
        21215: "Twilio does not have permission to dial that number. Check your account's geographic permissions.",
        21216: "Twilio account not authorized for this number. On trial accounts verify it at twilio.com/console → Verified Caller IDs.",
        21217: "The dialled number is not reachable via Twilio. Confirm the number is correct.",
        21218: "The From number does not have voice capability. Use a voice-capable Twilio number.",
        21219: "The destination is not a valid dialable phone number. Check the number.",
        21401: "Twilio could not parse the phone number. Ensure it starts with + and includes the country code.",
        32016: "Twilio cannot locate your calling app. Check that your Twilio voice webhook is configured.",
    }

    if code in guidance:
        return guidance[code]

    lower = raw.lower()

    # Generic 403 / "Unable to create record" — catch BEFORE trial-account check
    # because trial-account 403s also contain "unable to create record"
    if status == 403 or "unable to create record" in lower or "http 403" in lower:
        return (
            "Twilio rejected the request with HTTP 403. "
            "Most common causes: trial account (destination must be a Verified Caller ID — "
            "add it at twilio.com/console → Phone Numbers → Verified Caller IDs), "
            "geographic permissions not enabled (Twilio Console → Voice & Messaging → Geo Permissions), "
            "or the Twilio number lacks SMS/Voice capability."
        )

    # Trial-account verified-number restriction (SMS or voice)
    if ("unverified" in lower or "verified caller" in lower) and (
        "trial" in lower or "upgrade" in lower or "cannot" in lower
    ):
        return (
            "Twilio trial accounts can only text verified recipient numbers. "
            "Verify this customer in Twilio or upgrade the account."
        )

    if "authenticate" in lower or "authentication" in lower or "account sid" in lower:
        return "Twilio authentication failed. Check the Account SID and Auth Token in SMS Settings."

    if "not a valid phone number" in lower or "is not a valid" in lower:
        return "The phone number is invalid. Make sure it includes the country code (e.g. +15551234567)."

    if "geo permission" in lower or "geographic" in lower:
        return "Twilio geographic permissions block that destination. Enable the region in Twilio Console → Voice & Messaging → Geo Permissions."

    return raw
