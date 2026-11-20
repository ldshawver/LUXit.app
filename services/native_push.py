"""Alert pushes (texts, missed calls, voicemail) to LUX Connect native devices.

Incoming *calls* never go through here: Twilio delivers those itself as a
PushKit VoIP push (iOS) or an FCM data message (Android) using the Push
Credential bound into the Voice access token.

Providers, each enabled only when fully configured:

  APNs  LUXIT_APNS_KEY_FILE (.p8), LUXIT_APNS_KEY_ID, LUXIT_APNS_TEAM_ID,
        LUXIT_APNS_TOPIC (the iOS bundle id). Needs HTTP/2 (the ``h2`` package).
  FCM   LUXIT_FCM_SERVICE_ACCOUNT_FILE (Firebase service-account JSON), HTTP v1.

Android note: the Twilio Voice SDK owns the app's FirebaseMessagingService and
drops data-only messages that are not Twilio call invites. Texts are therefore
sent as FCM *notification* messages, which the system tray shows by itself
whenever the app is not in the foreground. In the foreground the app already
receives the same event over its live stream.

Sending happens on a short-lived background thread so a Twilio webhook never
waits on Apple or Google. Tokens the provider reports as dead are cleared.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# Calls ring through Twilio's VoIP push; an extra "Incoming call" alert would
# duplicate the native call screen.
SKIP_EVENT_TYPES = {"incoming_call"}

APNS_HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

_token_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


# ── configuration ─────────────────────────────────────────────────────────── #

def _read(path: str | None) -> str | None:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def apns_config() -> dict | None:
    key = _read(os.environ.get("LUXIT_APNS_KEY_FILE"))
    key_id = os.environ.get("LUXIT_APNS_KEY_ID")
    team_id = os.environ.get("LUXIT_APNS_TEAM_ID")
    topic = os.environ.get("LUXIT_APNS_TOPIC")
    if not (key and key_id and team_id and topic):
        return None
    return {"key": key, "key_id": key_id, "team_id": team_id, "topic": topic}


def fcm_config() -> dict | None:
    raw = _read(os.environ.get("LUXIT_FCM_SERVICE_ACCOUNT_FILE"))
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    if not all(info.get(k) for k in ("project_id", "client_email", "private_key")):
        return None
    return info


def http2_available() -> bool:
    try:
        import h2  # noqa: F401
        return True
    except ImportError:
        return False


def status() -> dict:
    """What can be delivered right now. Shown in /api/mobile/v1/me diagnostics."""
    return {
        "apns": bool(apns_config()) and http2_available(),
        "apns_configured": bool(apns_config()),
        "apns_http2": http2_available(),
        "fcm": bool(fcm_config()),
    }


# ── provider auth ─────────────────────────────────────────────────────────── #

def _cached(key: str, ttl: float, build):
    now = time.time()
    with _cache_lock:
        hit = _token_cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
    value = build()
    with _cache_lock:
        _token_cache[key] = (value, now + ttl)
    return value


def _apns_jwt(cfg: dict) -> str:
    import jwt

    def build():
        return jwt.encode({"iss": cfg["team_id"], "iat": int(time.time())}, cfg["key"],
                          algorithm="ES256", headers={"kid": cfg["key_id"]})
    # Apple rejects provider tokens older than an hour and throttles refreshing
    # more than every 20 minutes.
    return _cached(f"apns:{cfg['key_id']}", 45 * 60, build)


def _fcm_access_token(info: dict, client) -> str:
    import jwt

    def build():
        now = int(time.time())
        assertion = jwt.encode({
            "iss": info["client_email"], "scope": FCM_SCOPE,
            "aud": info.get("token_uri") or "https://oauth2.googleapis.com/token",
            "iat": now, "exp": now + 3600,
        }, info["private_key"], algorithm="RS256")
        r = client.post(info.get("token_uri") or "https://oauth2.googleapis.com/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion,
        }, timeout=10)
        r.raise_for_status()
        return r.json()["access_token"]
    return _cached(f"fcm:{info['client_email']}", 50 * 60, build)


# ── payloads ──────────────────────────────────────────────────────────────── #

def _deep_link(link: str | None) -> dict:
    """Translate a PWA link into the app's route params."""
    link = link or ""
    m = re.search(r"[?&]conv=(\d+)", link)
    if m:
        return {"route": "conversation", "conversation_id": m.group(1)}
    if "voicemail" in link:
        return {"route": "voicemail"}
    if "calls" in link or "recents" in link:
        return {"route": "recents"}
    return {"route": "messages"}


def build_messages(job: dict) -> tuple[dict, dict]:
    """Return (apns_payload, fcm_message_without_token) for one notification."""
    data = {"event_type": job["event_type"], **_deep_link(job.get("link"))}
    if job.get("phone_number_id") is not None:
        data["phone_number_id"] = str(job["phone_number_id"])
    thread = data.get("conversation_id") or job["event_type"]
    aps = {
        "alert": {"title": job["title"][:120], "body": job["body"][:400]},
        "sound": "default",
        "thread-id": f"lux-{thread}",
    }
    if isinstance(job.get("badge"), int):
        aps["badge"] = job["badge"]
    apns_payload = {"aps": aps, **data}
    channel = "messages" if job["event_type"] in ("incoming_sms", "new_message", "sms", "unread_message_reminder") else "calls"
    fcm = {
        "notification": {"title": job["title"][:120], "body": job["body"][:400]},
        "data": {k: str(v) for k, v in data.items()},
        "android": {
            "priority": "high",
            "collapse_key": f"lux-{thread}",
            "notification": {"channel_id": channel, "tag": f"lux-{thread}", "sound": "default",
                             **({"notification_count": job["badge"]} if isinstance(job.get("badge"), int) else {})},
        },
    }
    return apns_payload, fcm


# ── delivery ──────────────────────────────────────────────────────────────── #

def _send_apns(client, cfg: dict, device: dict, payload: dict, collapse: str) -> str:
    host = APNS_HOSTS["sandbox" if device.get("apns_environment") == "sandbox" else "production"]
    r = client.post(f"{host}/3/device/{device['push_token']}", json=payload, timeout=10, headers={
        "authorization": f"bearer {_apns_jwt(cfg)}",
        "apns-topic": cfg["topic"], "apns-push-type": "alert", "apns-priority": "10",
        "apns-collapse-id": collapse[:64],
    })
    if r.status_code == 200:
        return "sent"
    reason = ""
    try:
        reason = r.json().get("reason", "")
    except ValueError:
        pass
    if r.status_code == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
        return "dead"
    logger.warning("APNs send failed status=%s reason=%s device_id=%s", r.status_code, reason, device["id"])
    return "error"


def _send_fcm(client, info: dict, device: dict, message: dict) -> str:
    access = _fcm_access_token(info, client)
    r = client.post(
        f"https://fcm.googleapis.com/v1/projects/{info['project_id']}/messages:send",
        json={"message": {**message, "token": device["push_token"]}},
        headers={"authorization": f"Bearer {access}"}, timeout=10,
    )
    if r.status_code == 200:
        return "sent"
    detail = r.text[:300]
    if r.status_code == 404 or "UNREGISTERED" in detail or ("INVALID_ARGUMENT" in detail and "registration" in detail.lower()):
        return "dead"
    logger.warning("FCM send failed status=%s device_id=%s detail=%s", r.status_code, device["id"], detail)
    return "error"


def deliver(job: dict, devices: list[dict]) -> dict:
    """Send one notification to plain-dict devices. Returns {sent, dead_ids, errors}."""
    import httpx

    apns_payload, fcm_message = build_messages(job)
    collapse = apns_payload["aps"]["thread-id"]
    out = {"sent": 0, "dead_ids": [], "errors": 0}
    apns_cfg = apns_config()
    fcm_info = fcm_config()
    apns_client = None
    try:
        with httpx.Client() as http1:
            for device in devices:
                try:
                    if device["push_token_type"] == "apns":
                        if not apns_cfg or not http2_available():
                            continue
                        if apns_client is None:
                            apns_client = httpx.Client(http2=True)
                        result = _send_apns(apns_client, apns_cfg, device, apns_payload, collapse)
                    elif device["push_token_type"] == "fcm":
                        if not fcm_info:
                            continue
                        result = _send_fcm(http1, fcm_info, device, fcm_message)
                    else:
                        continue
                except Exception:
                    logger.exception("Native push failed device_id=%s", device.get("id"))
                    result = "error"
                if result == "sent":
                    out["sent"] += 1
                elif result == "dead":
                    out["dead_ids"].append(device["id"])
                else:
                    out["errors"] += 1
    finally:
        if apns_client is not None:
            apns_client.close()
    return out


def eligible_devices(company_id: int, user_id: int) -> list[dict]:
    from models import MobileDevice
    rows = MobileDevice.query.filter(
        MobileDevice.company_id == company_id,
        MobileDevice.user_id == user_id,
        MobileDevice.status == "active",
        MobileDevice.push_token.isnot(None),
    ).all()
    return [{"id": d.id, "push_token": d.push_token, "push_token_type": d.push_token_type,
             "apns_environment": d.apns_environment} for d in rows]


def _clear_dead_tokens(device_ids: list[int]) -> None:
    from extensions import db
    from models import MobileDevice
    if not device_ids:
        return
    for d in MobileDevice.query.filter(MobileDevice.id.in_(device_ids)).all():
        logger.info("Native push token retired device_id=%s type=%s", d.id, d.push_token_type)
        d.push_token = None
        d.push_token_updated_at = datetime.utcnow()
    db.session.commit()


# Tests set this to run delivery inline.
SYNC_FOR_TESTS = False


def queue_for_user(company_id: int, user_id: int, *, title: str, body: str, link: str | None,
                   event_type: str, badge, silent: bool, phone_number_id=None) -> int:
    """Look up the user's native devices now; deliver on a background thread.

    Returns the number of devices targeted (0 when nothing is configured, the
    event is excluded, or the user has no native device).
    """
    if silent or event_type in SKIP_EVENT_TYPES:
        return 0
    st = status()
    if not (st["apns"] or st["fcm"]):
        return 0
    devices = eligible_devices(company_id, user_id)
    if not devices:
        return 0
    job = {"title": title or "", "body": body or "", "link": link, "event_type": event_type,
           "badge": badge, "phone_number_id": phone_number_id}

    from flask import current_app
    app = current_app._get_current_object()

    def run():
        result = deliver(job, devices)
        if result["dead_ids"]:
            with app.app_context():
                try:
                    _clear_dead_tokens(result["dead_ids"])
                except Exception:
                    logger.exception("Native push token cleanup failed")
        logger.info("Native push result company_id=%s user_id=%s event=%s sent=%s dead=%s errors=%s",
                    company_id, user_id, event_type, result["sent"], len(result["dead_ids"]), result["errors"])

    if SYNC_FOR_TESTS:
        run()
    else:
        threading.Thread(target=run, name="lux-native-push", daemon=True).start()
    return len(devices)
