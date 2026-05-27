---
name: Twilio _sanitize_body latin-1 bug
description: Five copies of _sanitize_body across the codebase used latin-1 encode/decode, causing UnicodeEncodeError on emoji/special chars. All fixed.
---

The pattern `.encode("latin-1", errors="replace").decode("latin-1")` appeared in:
- `twilio_sms.py`
- `inbox_pwa.py`
- `sms_service.py`
- `services/sms_service.py`
- `services/integrations/twilio_service.py`

**Why it failed:** Latin-1 silently replaces characters outside its range with `?`, and raises on edge cases. Twilio accepts UTF-8 natively.

**Fix:** Replace each with just `body.replace("\x00", "")` — only strip null bytes, pass everything else through as UTF-8.

**How to apply:** If a new SMS helper is added anywhere, use the null-byte-only sanitize pattern. Never restrict to latin-1.
