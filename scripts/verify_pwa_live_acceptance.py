#!/usr/bin/env python3
"""Executable live VPS acceptance checks for LUXit PWA communications.

This script intentionally requires live IDs/cookies for checks that touch
user-scoped resources. It reports PASS/FAIL/SKIP with exact routes so deployment
verification is repeatable and failures are visible instead of hidden in notes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REQUIRED_VAPID = {"VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"}


class CheckRunner:
    def __init__(self, base_url: str, cookie: str = "", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie
        self.timeout = timeout
        self.failed = 0
        self.skipped = 0

    def _request(self, method: str, path: str, *, data=None, headers=None):
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        body = None
        request_headers = dict(headers or {})
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        if data is not None:
            if isinstance(data, (dict, list)):
                body = json.dumps(data).encode()
                request_headers.setdefault("Content-Type", "application/json")
            else:
                body = data
        req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def pass_(self, name: str, detail: str):
        print(f"PASS {name}: {detail}")

    def fail(self, name: str, detail: str):
        self.failed += 1
        print(f"FAIL {name}: {detail}")

    def skip(self, name: str, detail: str):
        self.skipped += 1
        print(f"SKIP {name}: {detail}")

    def check(self, name: str, ok: bool, detail: str):
        (self.pass_ if ok else self.fail)(name, detail)

    def json_request(self, method: str, path: str, *, data=None):
        status, headers, body = self._request(method, path, data=data)
        try:
            parsed = json.loads(body.decode() or "{}")
        except Exception:
            parsed = {"_raw": body.decode(errors="replace")[:500]}
        return status, headers, parsed


def load_cookie(cookie_arg: str, cookie_file: str) -> str:
    if cookie_arg:
        return cookie_arg
    if cookie_file:
        return Path(cookie_file).read_text(encoding="utf-8").strip()
    return os.environ.get("LUXIT_COOKIE", "")


def check_voice_inbound(runner: CheckRunner, to_number: str, from_number: str):
    form = urllib.parse.urlencode({
        "To": to_number,
        "From": from_number,
        "CallSid": "LIVE_VERIFY_VOICE_INBOUND",
        "Direction": "inbound",
    }).encode()
    status, headers, body = runner._request("POST", "/twilio/voice/inbound", data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
    text = body.decode(errors="replace")
    runner.check(
        "/twilio/voice/inbound",
        status == 200 and "<Response" in text and "Internal Server Error" not in text,
        f"status={status} content_type={headers.get('Content-Type')} body={text[:160]!r}",
    )


def check_push_status(runner: CheckRunner):
    status, _, body = runner.json_request("GET", "/api/pwa/push/status")
    missing = set(body.get("missing") or [])
    configured = bool(body.get("configured"))
    exact_missing = missing.issubset(REQUIRED_VAPID) and (configured or bool(missing))
    runner.check(
        "push setup endpoint",
        status == 200 and exact_missing,
        f"status={status} configured={configured} missing={sorted(missing)} message={body.get('message')!r}",
    )


def check_preferences(runner: CheckRunner):
    payload = {
        "text_alerts": True,
        "call_alerts": True,
        "voicemail_alerts": True,
        "unread_reminder_alerts": True,
        "sound_enabled": True,
        "vibration_enabled": True,
        "business_hours_only": True,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
        "unread_reminder_minutes": 1,
    }
    status, _, body = runner.json_request("PATCH", "/api/pwa/preferences", data=payload)
    prefs = body.get("preferences") or body
    ok = status == 200 and body.get("success") is not False and prefs.get("sound_enabled") is True and prefs.get("vibration_enabled") is True
    runner.check("alert preference save", ok, f"status={status} response={body}")


def check_voicemail_audio(runner: CheckRunner, call_id: str):
    if not call_id:
        runner.skip("voicemail audio proxy", "set --call-id or LUXIT_CALL_ID to verify playback without Twilio login")
        return
    status, headers, body = runner._request("GET", f"/api/calls/{call_id}/voicemail/audio")
    content_type = headers.get("Content-Type", "")
    looks_audio = status == 200 and body and "text/html" not in content_type.lower() and b"Twilio" not in body[:500]
    runner.check("voicemail audio proxy", looks_audio, f"status={status} content_type={content_type} bytes={len(body)}")


def check_greetings(runner: CheckRunner, phone_number_id: str, create: bool):
    if not phone_number_id:
        runner.skip("greeting list/create/activate", "set --phone-number-id or LUXIT_PHONE_NUMBER_ID")
        return
    status, _, body = runner.json_request("GET", f"/api/phone/numbers/{phone_number_id}/greetings")
    runner.check("greeting list", status == 200 and body.get("success") is not False, f"status={status} response={body}")
    if not create:
        runner.skip("greeting create/activate", "pass --write-tests to create and activate a VPS verification greeting")
        return
    payload = {
        "name": "VPS verification greeting",
        "greeting_type": "standard",
        "text_body": "Thank you for calling LUXit. Please leave a message.",
        "applies_to": "voicemail_default",
        "is_active": False,
    }
    status, _, body = runner.json_request("POST", f"/api/phone/numbers/{phone_number_id}/greetings", data=payload)
    greeting = body.get("greeting") or {}
    gid = greeting.get("id")
    runner.check("greeting create", status in {200, 201} and bool(gid), f"status={status} response={body}")
    if gid:
        status, _, body = runner.json_request("POST", f"/api/phone/greetings/{gid}/activate")
        runner.check("greeting activate", status == 200 and body.get("success") is True, f"status={status} response={body}")


def check_reminders(runner: CheckRunner, live: bool):
    status, _, body = runner.json_request("POST", "/api/pwa/reminders/unread/run?dry_run=1")
    runner.check("unread reminder dry-run", status == 200 and body.get("dry_run") is True and "would_create" in body, f"status={status} response={body}")
    if live:
        status, _, body = runner.json_request("POST", "/api/pwa/reminders/unread/run")
        runner.check("unread reminder live run", status == 200 and body.get("dry_run") is False and "created" in body, f"status={status} response={body}")
    else:
        runner.skip("unread reminder live run", "pass --run-live-reminder after creating a safe unread test conversation")


def check_contact_names(runner: CheckRunner, phone: str, expected: str):
    if not phone or not expected:
        runner.skip("Google/CRM contact name display", "set --contact-phone and --expected-contact-name")
        return
    status, _, body = runner.json_request("GET", "/api/inbox/conversations?filter=all")
    conversations = body.get("conversations") or []
    match = next((c for c in conversations if c.get("from_number") == phone or c.get("to_number") == phone), None)
    name = (match or {}).get("display_name") or (match or {}).get("contact_name")
    runner.check("Google/CRM contact name display", status == 200 and name == expected, f"status={status} phone={phone} expected={expected!r} actual={name!r}")


def check_theme_files(runner: CheckRunner):
    files = [
        Path("templates/inbox_pwa/index.html"),
        Path("templates/inbox_pwa/calls.html"),
    ]
    text = "\n".join(p.read_text(encoding="utf-8") for p in files if p.exists())
    required = ["--pwa-primary", "--pwa-card-bg", "--pwa-surface", "--pwa-border", "--pwa-text", "--pwa-muted"]
    runner.check("PWA shared theme variables", all(token in text for token in required), "checked inbox/calls templates for shared --pwa-* variables")
    purple_hits = [token for token in ["#6f42c1", "#7c3aed", "bg-purple", "btn-purple"] if token.lower() in text.lower()]
    runner.check("no hardcoded purple PWA controls", not purple_hits, f"hits={purple_hits}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run executable live VPS checks for PWA communications acceptance")
    parser.add_argument("--base-url", default=os.environ.get("LUXIT_BASE_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--cookie", default="", help="Authenticated Cookie header value, e.g. 'session=...'")
    parser.add_argument("--cookie-file", default=os.environ.get("LUXIT_COOKIE_FILE", "/tmp/luxit.cookies"))
    parser.add_argument("--voice-to", default=os.environ.get("LUXIT_VERIFY_VOICE_TO", "+19165989519"))
    parser.add_argument("--voice-from", default=os.environ.get("LUXIT_VERIFY_VOICE_FROM", "+14155551212"))
    parser.add_argument("--call-id", default=os.environ.get("LUXIT_CALL_ID", ""))
    parser.add_argument("--phone-number-id", default=os.environ.get("LUXIT_PHONE_NUMBER_ID", ""))
    parser.add_argument("--contact-phone", default=os.environ.get("LUXIT_CONTACT_PHONE", ""))
    parser.add_argument("--expected-contact-name", default=os.environ.get("LUXIT_EXPECTED_CONTACT_NAME", ""))
    parser.add_argument("--write-tests", action="store_true", help="Allow safe test writes such as creating/activating a verification greeting")
    parser.add_argument("--run-live-reminder", action="store_true", help="Create reminder rows for current unresolved unread conversations")
    args = parser.parse_args()

    cookie = load_cookie(args.cookie, args.cookie_file if Path(args.cookie_file).exists() else "")
    runner = CheckRunner(args.base_url, cookie=cookie)
    check_voice_inbound(runner, args.voice_to, args.voice_from)
    check_push_status(runner)
    check_preferences(runner)
    check_voicemail_audio(runner, args.call_id)
    check_greetings(runner, args.phone_number_id, args.write_tests)
    check_reminders(runner, args.run_live_reminder)
    check_contact_names(runner, args.contact_phone, args.expected_contact_name)
    check_theme_files(runner)
    print(f"SUMMARY failed={runner.failed} skipped={runner.skipped}")
    return 1 if runner.failed else 0


if __name__ == "__main__":
    sys.exit(main())
