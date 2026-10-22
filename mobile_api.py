"""LUX Connect native app API — ``/api/mobile/v1``.

Bearer-only and CSRF-exempt. Safe because a request is authenticated ONLY by
its ``Authorization: Bearer`` access token: the cookie session is never read
(Flask-Login's per-request user is set from the token before any view runs) and
never written (Set-Cookie is stripped). A cross-site page cannot attach a
bearer token, so there is nothing for CSRF to forge.

Most routes are the existing PWA JSON views mounted a second time under this
prefix, so tenancy, permissions, STOP/opt-out, consent and idempotency stay in
one implementation. Path mapping: ``/api/<x>`` on the web is
``/api/mobile/v1/<x>`` here (``/api/inbox/conversations`` ->
``/api/mobile/v1/inbox/conversations``). The routes defined in this module are
the ones the native app needs that the web does not have: sign-in, devices,
the native Voice token, MMS upload, the unified send, sync and number admin.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, g, jsonify, request

from extensions import db, limiter

logger = logging.getLogger(__name__)

mobile_api_bp = Blueprint("mobile_api", __name__, url_prefix="/api/mobile/v1")
mobile_media_public_bp = Blueprint("mobile_media_public", __name__)

API_VERSION = 1
PUBLIC_ENDPOINTS = {"mobile_api.auth_login", "mobile_api.auth_refresh", "mobile_api.app_config"}

MEDIA_MAX_BYTES = 5 * 1024 * 1024           # Twilio MMS limit per message
MEDIA_PUBLIC_WINDOW = timedelta(hours=4)    # how long Twilio may fetch it
MEDIA_RETENTION = timedelta(days=30)
MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/heic", "image/webp",
               "video/mp4", "video/3gpp", "audio/mpeg", "audio/mp4", "application/pdf", "text/vcard"}
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,100}$")


def _err(code: str, message: str, status: int):
    return jsonify({"success": False, "code": code, "error": message}), status


def _client_ip() -> str | None:
    # Same source as the web rate limits (extensions._client_ip): nginx
    # overwrites X-Real-IP. X-Forwarded-For keeps whatever the client sent as
    # its first hop, so keying throttles on it lets a caller pick a fresh IP
    # per attempt.
    from extensions import _client_ip as proxied_client_ip
    return (proxied_client_ip() or "")[:64] or None


# ── authentication for every request ──────────────────────────────────────── #

@mobile_api_bp.before_request
def _authenticate_bearer():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    from services.mobile_auth import MobileAuthError, authenticate_access_token
    header = request.headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        return _err("auth_required", "Sign in required.", 401)
    try:
        ctx = authenticate_access_token(header[7:].strip())
    except MobileAuthError as exc:
        return _err(exc.code, exc.message, exc.status)
    # Bind the user for this request only. Flask-Login resolves current_user
    # from g._login_user first, so the shared PWA views see this user and the
    # cookie session is never consulted.
    g._login_user = ctx.user
    g.mobile = ctx
    from inbox_pwa import _get_company
    current = _get_company(ctx.user)
    if not current or current.id != ctx.company.id:
        # The user's default company changed since sign-in; the shared views
        # would act on a different tenant than this session. Make the app sign
        # in again rather than mixing tenants.
        return _err("company_changed", "Your company changed. Please sign in again.", 401)
    return None


@mobile_api_bp.after_request
def _no_cookies(response):
    response.headers.pop("Set-Cookie", None)
    response.headers["Cache-Control"] = "no-store"
    return response


@mobile_api_bp.errorhandler(401)
@mobile_api_bp.errorhandler(403)
@mobile_api_bp.errorhandler(404)
@mobile_api_bp.errorhandler(409)
@mobile_api_bp.errorhandler(429)
def _json_http_error(exc):
    code = getattr(exc, "code", 500)
    return _err({401: "auth_required", 403: "forbidden", 404: "not_found", 409: "conflict",
                 429: "too_many_attempts"}.get(code, "error"),
                getattr(exc, "description", None) or "Request failed.", code)


# ── public: config, sign-in, refresh ──────────────────────────────────────── #

@mobile_api_bp.route("/config")
def app_config():
    return jsonify({
        "success": True,
        "api_version": API_VERSION,
        "environment": os.environ.get("APP_ENV") or os.environ.get("LUXIT_ENVIRONMENT") or "production",
        "min_app_version": os.environ.get("LUX_CONNECT_MIN_APP_VERSION") or "1.0.0",
    })


def _device_json(device) -> dict:
    return {
        "id": device.id,
        "platform": device.platform,
        "device_name": device.device_name,
        "model": device.model,
        "app_version": device.app_version,
        "status": device.status,
        "ring_enabled": bool(device.ring_enabled),
        "push_registered": bool(device.push_token),
        "voice_registered": bool(device.voice_registered_at),
        "voice_registered_at": device.voice_registered_at.isoformat() + "Z" if device.voice_registered_at else None,
        "last_seen_at": device.last_seen_at.isoformat() + "Z" if device.last_seen_at else None,
        "is_current": bool(getattr(g, "mobile", None) and g.mobile.device.id == device.id),
    }


@mobile_api_bp.route("/auth/login", methods=["POST"])
@limiter.limit("10 per minute;50 per hour", methods=["POST"])
def auth_login():
    from services.mobile_auth import (MobileAuthError, clear_failures, is_throttled, issue_session,
                                      record_failure, resolve_company_for, upsert_device, verify_credentials)
    data = request.get_json(silent=True) or {}
    identifier = (data.get("email") or data.get("username") or "").strip()
    ip = _client_ip()
    if is_throttled(identifier, ip):
        return _err("too_many_attempts", "Too many sign-in attempts. Wait 15 minutes and try again.", 429)
    try:
        user = verify_credentials(identifier, data.get("password") or "")
    except MobileAuthError as exc:
        if exc.code == "invalid_credentials" and identifier:
            record_failure(identifier, ip)
        return _err(exc.code, exc.message, exc.status)
    try:
        company = resolve_company_for(user)
        device = upsert_device(user, company, data.get("device") or {})
        tokens = issue_session(user, company, device, ip=ip)
    except MobileAuthError as exc:
        db.session.rollback()
        return _err(exc.code, exc.message, exc.status)
    clear_failures(identifier)
    db.session.commit()
    logger.info("LUX Connect sign-in user_id=%s company_id=%s device_id=%s platform=%s",
                user.id, company.id, device.id, device.platform)
    return jsonify({"success": True, **tokens.as_json(), "device": _device_json(device)})


@mobile_api_bp.route("/auth/refresh", methods=["POST"])
@limiter.limit("30 per minute", methods=["POST"])
def auth_refresh():
    from services.mobile_auth import MobileAuthError, rotate_refresh_token
    data = request.get_json(silent=True) or {}
    try:
        tokens, _ctx = rotate_refresh_token((data.get("refresh_token") or "").strip(), ip=_client_ip())
    except MobileAuthError as exc:
        db.session.rollback()
        return _err(exc.code, exc.message, exc.status)
    return jsonify({"success": True, **tokens.as_json()})


@mobile_api_bp.route("/auth/logout", methods=["POST"])
def auth_logout():
    """Sign this device out: stop ringing it at once and drop its push token."""
    from services.mobile_auth import revoke_device_sessions
    device = g.mobile.device
    device.status = "signed_out"
    device.push_token = None
    device.voice_registered_at = None
    device.voice_token_hash = None
    revoke_device_sessions(device, "logout")
    db.session.commit()
    logger.info("LUX Connect sign-out device_id=%s user_id=%s", device.id, device.user_id)
    return jsonify({"success": True})


# ── account summary ───────────────────────────────────────────────────────── #

def _push_credential_sid(device) -> str | None:
    if device.platform == "android":
        return os.environ.get("TWILIO_PUSH_CREDENTIAL_SID_ANDROID") or None
    if device.apns_environment == "sandbox":
        return os.environ.get("TWILIO_PUSH_CREDENTIAL_SID_IOS_SANDBOX") or None
    return os.environ.get("TWILIO_PUSH_CREDENTIAL_SID_IOS") or None


def _calling_status(user, company, device) -> dict:
    """Why calling is or is not available, without minting a token."""
    from inbox_pwa import _voice_token_gate
    gate, denied = _voice_token_gate(user, company)
    if denied:
        body = denied[0].get_json() or {}
        return {"available": False, "reason": body.get("code"), "message": body.get("error"),
                "incoming": False, "outgoing": False}
    if device.status == "pending_approval":
        return {"available": False, "reason": "DEVICE_PENDING_APPROVAL",
                "message": "An administrator must approve this phone before it can take calls.",
                "incoming": False, "outgoing": False}
    incoming = bool(_push_credential_sid(device))
    outgoing = bool(os.environ.get("TWILIO_TWIML_APP_SID"))
    return {
        "available": incoming or outgoing,
        "reason": None if incoming else "PUSH_CREDENTIAL_NOT_CONFIGURED",
        "message": None if incoming else "Incoming calls to this phone are not set up on the server yet.",
        "incoming": incoming,
        "outgoing": outgoing,
        "calling_number": gate.allowed_numbers[0] if gate.allowed_numbers else None,
    }


@mobile_api_bp.route("/me")
def me():
    from services import native_push
    from services.comms_permissions import can_manage_users
    from services.lux_numbers import numbers_for_user
    from services.phone_availability import get_availability
    from services.receive_calls import get_receive_calls
    from inbox_pwa import phone_settings_editable
    ctx = g.mobile
    user, company, device = ctx.user, ctx.company, ctx.device
    return jsonify({
        "success": True,
        "user": {"id": user.id,
                 "name": getattr(user, "full_name", None) or getattr(user, "username", None) or user.email,
                 "email": user.email},
        "company": {"id": company.id, "name": company.name},
        "is_admin": bool(can_manage_users(user, company.id)),
        "device": _device_json(device),
        "numbers": _numbers_with_routing(user, company.id),
        "can_edit_phone_settings": phone_settings_editable(user, company.id),
        "availability": get_availability(user.id, company.id),
        "receive_calls": get_receive_calls(user.id, company.id),
        "calling": _calling_status(user, company, device),
        "push": native_push.status(),
        "server_time": datetime.utcnow().isoformat() + "Z",
    })


def _mask_number(number: str | None, reveal: bool) -> str | None:
    if not number or reveal:
        return number
    return "•••" + number[-4:]


def _routing_for(phone_number: str, reveal: bool) -> dict | None:
    """Where calls to this number go (same decision as the voice webhook).
    Forwarding destinations are masked for users who cannot edit them."""
    from twilio_sms import number_call_routing_status
    try:
        status = number_call_routing_status(phone_number)
    except Exception:
        logger.exception("call routing status failed")
        return None
    if not status:
        return None
    for key in ("business_hours", "after_hours", "now"):
        status[key] = {**status[key], "forward_to": _mask_number(status[key]["forward_to"], reveal)}
    return status


def _numbers_with_routing(user, company_id: int) -> list[dict]:
    from inbox_pwa import phone_settings_editable
    from services.lux_numbers import numbers_for_user
    reveal = phone_settings_editable(user, company_id)
    rows = numbers_for_user(user, company_id)
    for n in rows:
        n["call_routing"] = _routing_for(n["phone_number"], reveal)
    return rows


@mobile_api_bp.route("/phone/numbers/<int:number_id>/routing")
def number_routing(number_id):
    from inbox_pwa import phone_settings_editable
    from models import TwilioPhoneNumber
    from services.comms_permissions import accessible_phone_numbers
    ctx = g.mobile
    pn = TwilioPhoneNumber.query.filter_by(id=number_id, company_id=ctx.company.id, is_active=True).first()
    if not pn or pn.phone_number not in accessible_phone_numbers(ctx.user, ctx.company.id):
        return _err("not_found", "Number not found.", 404)
    return jsonify({"success": True, "routing": _routing_for(pn.phone_number, phone_settings_editable(ctx.user, ctx.company.id))})


# ── automation rules (shared validation: services/auto_reply_rules.py) ───── #

def _rules_scope():
    from inbox_pwa import _require_pwa_communications_access, phone_settings_editable
    from models import TwilioPhoneNumber
    from services.comms_permissions import accessible_phone_numbers
    ctx = g.mobile
    denied = _require_pwa_communications_access(ctx.user, ctx.company)
    if denied:
        return None, denied
    allowed = set(accessible_phone_numbers(ctx.user, ctx.company.id))
    ids = {n.id for n in TwilioPhoneNumber.query.filter_by(company_id=ctx.company.id).all() if n.phone_number in allowed}
    return {"ids": ids, "editable": phone_settings_editable(ctx.user, ctx.company.id)}, None


def _visible_rule(rule_id: int, scope):
    from models import AutoReplyRule
    rule = AutoReplyRule.query.filter_by(id=rule_id, company_id=g.mobile.company.id).first()
    if not rule or (rule.phone_number_id is not None and rule.phone_number_id not in scope["ids"]):
        return None
    return rule


@mobile_api_bp.route("/automation/rules", methods=["GET", "POST"])
def automation_rules():
    from models import AutoReplyRule
    from services import auto_reply_rules as svc
    scope, denied = _rules_scope()
    if denied:
        return denied
    company = g.mobile.company
    if request.method == "GET":
        q = AutoReplyRule.query.filter_by(company_id=company.id)
        number_id = request.args.get("phone_number_id", type=int)
        if number_id:
            q = q.filter(db.or_(AutoReplyRule.phone_number_id.is_(None), AutoReplyRule.phone_number_id == number_id))
        rules = [r for r in q.order_by(AutoReplyRule.priority.desc(), AutoReplyRule.id).all()
                 if r.phone_number_id is None or r.phone_number_id in scope["ids"]]
        return jsonify({"success": True, "editable": scope["editable"], "rules": [svc.to_dict(r) for r in rules]})
    if not scope["editable"]:
        return _err("forbidden", "Only an administrator can change automatic replies and rules.", 403)
    data = request.get_json(silent=True) or {}
    try:
        fields = svc.validate(company.id, data)
    except svc.RuleError as e:
        return _err(e.code, str(e), e.status)
    if fields["phone_number_id"] is not None and fields["phone_number_id"] not in scope["ids"]:
        return _err("not_found", "Unknown phone number.", 404)
    rule = AutoReplyRule(company_id=company.id, **fields)
    db.session.add(rule)
    db.session.commit()
    logger.info("LUX Connect rule %s created by user_id=%s", rule.id, g.mobile.user.id)
    return jsonify({"success": True, "rule": svc.to_dict(rule)}), 201


@mobile_api_bp.route("/automation/rules/<int:rule_id>", methods=["GET", "PATCH", "DELETE"])
def automation_rule(rule_id):
    from services import auto_reply_rules as svc
    scope, denied = _rules_scope()
    if denied:
        return denied
    rule = _visible_rule(rule_id, scope)
    if not rule:
        return _err("not_found", "Rule not found.", 404)
    if request.method == "GET":
        return jsonify({"success": True, "editable": scope["editable"], "rule": svc.to_dict(rule)})
    if not scope["editable"]:
        return _err("forbidden", "Only an administrator can change automatic replies and rules.", 403)
    if rule.trigger_type == "stop_keyword":
        return _err("system_rule", "Opt-out handling can't be changed from the app.", 409)
    if request.method == "DELETE":
        db.session.delete(rule)
        db.session.commit()
        return jsonify({"success": True})
    try:
        fields = svc.validate(g.mobile.company.id, request.get_json(silent=True) or {}, existing=rule)
    except svc.RuleError as e:
        return _err(e.code, str(e), e.status)
    if fields["phone_number_id"] is not None and fields["phone_number_id"] not in scope["ids"]:
        return _err("not_found", "Unknown phone number.", 404)
    for k, v in fields.items():
        setattr(rule, k, v)
    db.session.commit()
    return jsonify({"success": True, "rule": svc.to_dict(rule)})


# ── devices ───────────────────────────────────────────────────────────────── #

def _retire_other_holders(device, *, push_token: str | None = None, voice_token_hash: str | None = None):
    """A handset can only belong to one signed-in user at a time.

    If another row (another user, or an older install) still holds this
    handset's push token or Voice binding, sign it out so it is never dialed
    or notified on someone else's phone.
    """
    from models import MobileDevice
    q = MobileDevice.query.filter(MobileDevice.id != device.id)
    if push_token:
        others = q.filter(MobileDevice.push_token == push_token).all()
    elif voice_token_hash:
        others = q.filter(MobileDevice.voice_token_hash == voice_token_hash).all()
    else:
        return 0
    from services.mobile_auth import revoke_device_sessions
    for other in others:
        other.push_token = None
        other.voice_token_hash = None
        other.voice_registered_at = None
        if other.status in ("active", "pending_approval"):
            other.status = "signed_out"
            revoke_device_sessions(other, "handset_reassigned")
        logger.info("LUX Connect handset reassigned: retired device_id=%s (now device_id=%s)", other.id, device.id)
    return len(others)


@mobile_api_bp.route("/devices/current", methods=["POST", "PATCH"])
def device_current_update():
    device = g.mobile.device
    data = request.get_json(silent=True) or {}
    for field, limit in (("device_name", 120), ("model", 120), ("os_version", 40), ("app_version", 40)):
        if field in data:
            setattr(device, field, (str(data.get(field) or "").strip()[:limit] or None))
    if "ring_enabled" in data:
        device.ring_enabled = bool(data.get("ring_enabled"))
    if data.get("apns_environment") in ("sandbox", "production"):
        device.apns_environment = data["apns_environment"]
    device.last_seen_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "device": _device_json(device)})


@mobile_api_bp.route("/devices/current/push-token", methods=["PUT"])
def device_push_token():
    device = g.mobile.device
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    token_type = (data.get("type") or "").strip().lower()
    if not token:
        device.push_token = None
    else:
        if token_type not in ("apns", "fcm") or len(token) > 512 or not re.match(r"^[A-Za-z0-9:_\-.]+$", token):
            return _err("invalid_push_token", "Push token is not valid.", 400)
        if (device.platform == "ios") != (token_type == "apns"):
            return _err("invalid_push_token", "Push token type does not match this device.", 400)
        _retire_other_holders(device, push_token=token)
        device.push_token = token
        device.push_token_type = token_type
        if data.get("apns_environment") in ("sandbox", "production"):
            device.apns_environment = data["apns_environment"]
    device.push_token_updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "device": _device_json(device)})


@mobile_api_bp.route("/devices/current/voice-registration", methods=["POST"])
def device_voice_registration():
    """The app reports the outcome of Voice.register()/unregister()."""
    device = g.mobile.device
    data = request.get_json(silent=True) or {}
    voice_token = (data.get("voice_device_token") or "").strip()
    if data.get("registered"):
        if voice_token:
            token_hash = hashlib.sha256(voice_token.encode("utf-8")).hexdigest()
            _retire_other_holders(device, voice_token_hash=token_hash)
            device.voice_token_hash = token_hash
        device.voice_registered_at = datetime.utcnow()
        device.voice_registration_error = None
    else:
        device.voice_registered_at = None
        device.voice_registration_error = (str(data.get("error") or "unregistered"))[:300]
    db.session.commit()
    return jsonify({"success": True, "device": _device_json(device)})


def _manageable_device(device_id: int):
    from models import MobileDevice
    from services.comms_permissions import can_manage_users
    ctx = g.mobile
    device = MobileDevice.query.filter_by(id=device_id, company_id=ctx.company.id).first()
    if not device:
        abort(404, "Device not found.")
    if device.user_id != ctx.user.id and not can_manage_users(ctx.user, ctx.company.id):
        abort(404, "Device not found.")
    return device


@mobile_api_bp.route("/devices")
def devices_list():
    from models import MobileDevice
    from services.comms_permissions import can_manage_users
    ctx = g.mobile
    q = MobileDevice.query.filter_by(company_id=ctx.company.id)
    scope = request.args.get("scope")
    if scope != "company" or not can_manage_users(ctx.user, ctx.company.id):
        q = q.filter_by(user_id=ctx.user.id)
    rows = q.filter(MobileDevice.status.in_(("active", "pending_approval"))).order_by(MobileDevice.last_seen_at.desc()).all()
    return jsonify({"success": True, "devices": [{**_device_json(d), "user_id": d.user_id} for d in rows]})


@mobile_api_bp.route("/devices/<int:device_id>", methods=["PATCH"])
def devices_update(device_id):
    device = _manageable_device(device_id)
    data = request.get_json(silent=True) or {}
    if "ring_enabled" in data:
        device.ring_enabled = bool(data.get("ring_enabled"))
    if "device_name" in data:
        device.device_name = (str(data.get("device_name") or "").strip()[:120] or None)
    db.session.commit()
    return jsonify({"success": True, "device": _device_json(device)})


@mobile_api_bp.route("/devices/<int:device_id>", methods=["DELETE"])
def devices_revoke(device_id):
    """Sign a device out remotely (lost phone). It stops ringing immediately."""
    from services.mobile_auth import revoke_device_sessions
    device = _manageable_device(device_id)
    device.status = "revoked"
    device.revoked_at = datetime.utcnow()
    device.revoked_by_user_id = g.mobile.user.id
    device.push_token = None
    device.voice_registered_at = None
    device.voice_token_hash = None
    revoke_device_sessions(device, "revoked")
    db.session.commit()
    logger.info("LUX Connect device revoked device_id=%s by user_id=%s", device.id, g.mobile.user.id)
    return jsonify({"success": True})


@mobile_api_bp.route("/devices/<int:device_id>/approve", methods=["POST"])
def devices_approve(device_id):
    from services.comms_permissions import can_manage_users
    ctx = g.mobile
    if not can_manage_users(ctx.user, ctx.company.id):
        return _err("forbidden", "Only an administrator can approve devices.", 403)
    device = _manageable_device(device_id)
    if device.status != "pending_approval":
        return _err("not_pending", "This device is not waiting for approval.", 409)
    device.status = "active"
    db.session.commit()
    return jsonify({"success": True, "device": _device_json(device)})


# ── voice ─────────────────────────────────────────────────────────────────── #

@mobile_api_bp.route("/voice/token")
def voice_token():
    """Twilio Voice access token bound to THIS device's identity and push credential."""
    from inbox_pwa import _voice_token_gate
    from services.phone_identity import mobile_voice_identity
    ctx = g.mobile
    user, company, device = ctx.user, ctx.company, ctx.device
    gate, denied = _voice_token_gate(user, company)
    if denied:
        return denied
    if device.status != "active":
        return _err("DEVICE_PENDING_APPROVAL", "An administrator must approve this phone before it can take calls.", 403)
    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import VoiceGrant
    identity = mobile_voice_identity(company.id, user.id, device.id, device.device_uid)
    push_sid = _push_credential_sid(device)
    twiml_app_sid = os.environ.get("TWILIO_TWIML_APP_SID")
    ttl = 3600
    token = AccessToken(gate.account_sid, gate.api_key, gate.api_secret, identity=identity, ttl=ttl)
    grant_kwargs = {"incoming_allow": bool(push_sid), "outgoing_application_sid": twiml_app_sid}
    if push_sid:
        grant_kwargs["push_credential_sid"] = push_sid
    token.add_grant(VoiceGrant(**grant_kwargs))
    return jsonify({
        "success": True,
        "token": token.to_jwt(),
        "identity": identity,
        "expires_in": ttl,
        "calling_number": gate.allowed_numbers[0],
        "permitted_numbers": gate.allowed_numbers,
        "incoming_enabled": bool(push_sid),
        "outbound_enabled": bool(twiml_app_sid),
    })


# ── messaging: MMS upload + unified idempotent send ───────────────────────── #

def _public_base_url() -> str:
    base = (os.environ.get("TWILIO_WEBHOOK_PUBLIC_URL") or os.environ.get("LUXIT_TWILIO_WEBHOOK_BASE_URL")
            or request.host_url).rstrip("/")
    # The configured value may include a path (".../twilio"); media lives at the root.
    m = re.match(r"^(https?://[^/]+)", base)
    return m.group(1) if m else base


@mobile_api_bp.route("/media", methods=["POST"])
def media_upload():
    from models import MobileMediaUpload
    ctx = g.mobile
    f = request.files.get("file")
    if not f:
        return _err("no_file", "Attach a file.", 400)
    content_type = (f.mimetype or "").lower()
    if content_type not in MEDIA_TYPES:
        return _err("unsupported_media", "That type of attachment can't be sent by text.", 415)
    data = f.read(MEDIA_MAX_BYTES + 1)
    if not data:
        return _err("no_file", "The attachment is empty.", 400)
    if len(data) > MEDIA_MAX_BYTES:
        return _err("media_too_large", "Attachments must be 5 MB or smaller.", 413)
    row = MobileMediaUpload(
        company_id=ctx.company.id, user_id=ctx.user.id, public_token=secrets.token_urlsafe(32),
        content_type=content_type, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
        data=data, expires_at=datetime.utcnow() + MEDIA_RETENTION,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({"success": True, "media": {"id": row.id, "content_type": content_type, "size_bytes": len(data)}})


@mobile_api_bp.route("/messages", methods=["POST"])
def send_unified():
    """Send a text or MMS to a conversation or a phone number, exactly once.

    ``idempotency_key`` is required and must be generated once per composed
    message on the device and reused for every retry of it.
    """
    from inbox_pwa import (_can_send_sms_from_number, _get_twilio_account, _msg_to_dict,
                           _normalize_phone, _require_pwa_communications_access)
    from models import MobileMediaUpload, TwilioConversation, TwilioMessage, TwilioPhoneNumber
    from services.comms_permissions import filter_conversations_for_user
    from services.lux_numbers import messaging_blocked
    from services.sms_send_idempotency import send_with_idempotency

    ctx = g.mobile
    user, company = ctx.user, ctx.company
    denied = _require_pwa_communications_access(user, company)
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    key = str(data.get("idempotency_key") or "")
    if not IDEMPOTENCY_KEY_RE.match(key):
        return _err("idempotency_key_required", "idempotency_key (16-100 letters, digits, - or _) is required.", 400)
    body = (data.get("body") or "").strip()
    media_ids = data.get("media_ids") or []
    if not isinstance(media_ids, list) or len(media_ids) > 10:
        return _err("invalid_media", "Up to 10 attachments per message.", 400)
    if not body and not media_ids:
        return _err("invalid_message", "Type a message or attach something.", 422)
    if len(body) > 1600:
        return _err("invalid_message", "Messages are limited to 1600 characters.", 422)

    uploads = []
    for mid in media_ids:
        up = MobileMediaUpload.query.filter_by(id=int(mid) if str(mid).isdigit() else -1,
                                               company_id=company.id, user_id=user.id).first()
        if not up or up.created_at < datetime.utcnow() - MEDIA_PUBLIC_WINDOW:
            return _err("invalid_media", "An attachment expired. Attach it again.", 422)
        uploads.append(up)

    ta = _get_twilio_account(company.id)
    if not ta or not ta.is_configured:
        return _err("phone_line_not_found", "Texting is not set up for this company.", 404)

    if data.get("conversation_id"):
        conv = filter_conversations_for_user(
            TwilioConversation.query.filter_by(id=int(data["conversation_id"]), company_id=company.id),
            user, company.id).first()
        if not conv:
            return _err("conversation_not_found", "Conversation not found.", 404)
        if not conv.to_number or not _can_send_sms_from_number(user, company.id, conv.to_number):
            return _err("phone_line_forbidden", "You can't text from this number.", 403)
    else:
        to_raw = (data.get("to") or "").strip()
        to_num = _normalize_phone(to_raw) if to_raw else ""
        if len(re.sub(r"\D", "", to_num)) < 10:
            return _err("invalid_recipient", "Enter a valid phone number.", 422)
        sender_id = data.get("from_phone_number_id")
        if sender_id not in (None, ""):
            # Chosen sender: only an active number of this company the user may
            # text from. A company has one thread per customer and the thread
            # follows the line last used (as inbound texts do), so an existing
            # thread is moved to the chosen line.
            sender = TwilioPhoneNumber.query.filter_by(
                id=int(sender_id) if str(sender_id).isdigit() else -1,
                company_id=company.id, is_active=True).first()
            if not sender or not _can_send_sms_from_number(user, company.id, sender.phone_number):
                return _err("phone_line_forbidden", "You can't text from this number.", 403)
            conv = (TwilioConversation.query.filter_by(company_id=company.id, from_number=to_num).first()
                    or TwilioConversation.query.filter_by(company_id=company.id, from_number=to_raw).first())
            if conv:
                conv.to_number = sender.phone_number
                conv.phone_number_id = sender.id
            else:
                conv = TwilioConversation(company_id=company.id, from_number=to_num, phone_number_id=sender.id,
                                          to_number=sender.phone_number, is_read=True)
                db.session.add(conv)
                db.session.flush()
        else:
            if not ta.from_phone or not _can_send_sms_from_number(user, company.id, ta.from_phone):
                return _err("phone_line_forbidden", "You can't text from this number.", 403)
            conv = (TwilioConversation.query.filter_by(company_id=company.id, from_number=to_num).first()
                    or TwilioConversation.query.filter_by(company_id=company.id, from_number=to_raw).first())
            if not conv:
                conv = TwilioConversation(company_id=company.id, from_number=to_num,
                                          to_number=ta.from_phone or "", is_read=True)
                db.session.add(conv)
                db.session.flush()

    pn = TwilioPhoneNumber.query.filter_by(company_id=company.id, phone_number=conv.to_number).first()
    blocked = messaging_blocked(getattr(pn, "id", None))
    if blocked:
        db.session.rollback()
        return _err("messaging_not_registered",
                    "This number isn't approved for business texting yet (US carrier registration). Calls still work.", 422)

    base = _public_base_url()
    media_urls = [f"{base}/mobile-media/{up.public_token}" for up in uploads]
    db.session.commit()
    result = send_with_idempotency(
        company_id=company.id, user_id=user.id, conversation_id=conv.id, body=body,
        twilio_account=ta, idempotency_key="app:" + key, media_urls=media_urls or None,
    )
    if result.get("status") == "delivery_unknown":
        return _err("send_in_progress", result.get("error") or "Already sending.", 409)
    if not result.get("success"):
        code = "recipient_opted_out" if result.get("error_code") == "recipient_opted_out" else "provider_failure"
        return _err(code, result.get("error") or "Text failed to send.", 409 if code == "recipient_opted_out" else 502)
    record = TwilioMessage.query.filter_by(twilio_sid=result.get("sid")).first() if result.get("sid") else None
    if not result.get("idempotent"):
        preview = body[:150] if body else "Attachment"
        conv.last_message_at = datetime.utcnow()
        conv.last_message_preview = f"You: {preview}"
        conv.message_count = (conv.message_count or 0) + 1
        conv.is_read = True
        for up in uploads:
            up.attached_message_sid = result.get("sid")
        db.session.commit()
    return jsonify({"success": True, "idempotent": bool(result.get("idempotent")),
                    "conversation_id": conv.id, "message": _msg_to_dict(record) if record else None})


def _authorized_upload(token: str):
    from models import MobileMediaUpload
    up = MobileMediaUpload.query.filter_by(public_token=token).first()
    if not up or up.expires_at < datetime.utcnow():
        abort(404)
    return up


@mobile_media_public_bp.route("/mobile-media/<token>")
def media_public(token):
    """Twilio fetches outbound MMS media here. Long random token, short window."""
    up = _authorized_upload(token)
    if up.created_at < datetime.utcnow() - MEDIA_PUBLIC_WINDOW:
        abort(404)
    return Response(up.data, mimetype=up.content_type, headers={"Cache-Control": "no-store"})


# ── sync after reconnect ──────────────────────────────────────────────────── #

@mobile_api_bp.route("/sync")
def sync():
    """Everything that changed since ``since`` (ISO-8601, server clock)."""
    from inbox_pwa import _call_to_dict, _conv_to_dict, _pwa_badge_counts_for
    from models import TwilioCallLog, TwilioConversation
    from services.comms_permissions import filter_calls_for_user, filter_conversations_for_user
    ctx = g.mobile
    server_time = datetime.utcnow()
    raw = (request.args.get("since") or "").strip().rstrip("Z")
    try:
        since = datetime.fromisoformat(raw) if raw else server_time - timedelta(days=7)
    except ValueError:
        return _err("invalid_since", "since must be an ISO-8601 timestamp.", 400)
    # Overlap so a write committed just before the previous sync isn't missed.
    since = since - timedelta(seconds=30)
    convs = filter_conversations_for_user(
        TwilioConversation.query.filter(TwilioConversation.company_id == ctx.company.id,
                                        TwilioConversation.last_message_at >= since),
        ctx.user, ctx.company.id).order_by(TwilioConversation.last_message_at.desc()).limit(200).all()
    calls = filter_calls_for_user(
        TwilioCallLog.query.filter(TwilioCallLog.company_id == ctx.company.id,
                                   TwilioCallLog.created_at >= since),
        ctx.user, ctx.company.id).order_by(TwilioCallLog.created_at.desc()).limit(200).all()
    try:
        badges = _pwa_badge_counts_for(ctx.user, ctx.company)
    except Exception:
        badges = None
    return jsonify({
        "success": True,
        "server_time": server_time.isoformat() + "Z",
        "conversations": [_conv_to_dict(c) for c in convs],
        "calls": [_call_to_dict(c) for c in calls],
        "badges": badges,
    })


# ── number management (admins) ────────────────────────────────────────────── #

@mobile_api_bp.route("/admin/numbers")
def admin_numbers():
    from services.lux_numbers import NumberError, _require_admin, company_numbers
    ctx = g.mobile
    try:
        _require_admin(ctx.user, ctx.company.id)
    except NumberError as exc:
        return _err(exc.code, exc.message, exc.status)
    return jsonify({"success": True, "numbers": company_numbers(ctx.company.id)})


@mobile_api_bp.route("/admin/numbers/<int:number_id>/assignments/<int:user_id>", methods=["PUT", "DELETE"])
def admin_number_assignment(number_id, user_id):
    from services.lux_numbers import NumberError, assign_number, unassign_number
    ctx = g.mobile
    try:
        if request.method == "DELETE":
            unassign_number(ctx.user, ctx.company.id, number_id, user_id)
            return jsonify({"success": True})
        result = assign_number(ctx.user, ctx.company.id, number_id, user_id, request.get_json(silent=True) or {})
        return jsonify({"success": True, "assignment": result})
    except NumberError as exc:
        db.session.rollback()
        return _err(exc.code, exc.message, exc.status)


@mobile_api_bp.route("/admin/numbers/<int:number_id>", methods=["DELETE"])
def admin_number_release(number_id):
    return _err("release_not_supported",
                "Numbers can't be released from the app. Contact LUXit support to release or port a number.", 405)


# ── shared PWA views, mounted under the bearer-only prefix ────────────────── #

def _mount_shared_views():
    import inbox_pwa as v
    routes = [
        ("/inbox/conversations", v.list_conversations, ["GET"]),
        ("/inbox/conversations/<int:conv_id>", v.get_conversation, ["GET"]),
        ("/inbox/conversations/<int:conv_id>/messages", v.send_message, ["POST"]),
        ("/inbox/conversations/<int:conv_id>/read", v.mark_read, ["PATCH"]),
        ("/inbox/conversations/<int:conv_id>/archive", v.archive_conversation, ["PATCH"]),
        ("/inbox/conversations/<int:conv_id>/rename", v.rename_contact, ["PATCH"]),
        ("/inbox/conversations/<int:conv_id>/contact", v.update_conversation_contact, ["PATCH"]),
        ("/inbox/messages", v.list_messages, ["GET"]),
        ("/inbox/messages/<int:message_id>/media/<int:media_index>", v.api_inbox_message_media, ["GET"]),
        ("/inbox/media-uploads/<token>", v.api_inbox_media_upload, ["GET"]),
        ("/inbox/unread-count", v.unread_count, ["GET"]),
        ("/inbox/badge-counts", v.badge_counts, ["GET"]),
        ("/inbox/contacts/search", v.search_contacts, ["GET"]),
        ("/inbox/stream", v.sse_stream, ["GET"]),
        ("/calls/recent", v.api_recent_calls, ["GET"]),
        ("/calls/voicemails", v.api_voicemails, ["GET"]),
        ("/calls/<int:call_id>", v.api_call_detail, ["GET"]),
        ("/calls/<int:call_id>/mark-read", v.api_call_mark_read, ["POST"]),
        ("/calls/<int:call_id>/mark-unread", v.api_call_mark_unread, ["POST"]),
        ("/calls/<int:call_id>/archive", v.api_call_archive, ["POST"]),
        ("/calls/<int:call_id>/voicemail/audio", v.api_call_voicemail_audio, ["GET"]),
        ("/calls/<int:call_id>/accept", v.api_call_accept, ["POST"]),
        ("/calls/<int:call_id>/decline", v.api_call_decline, ["POST"]),
        ("/calls/<int:call_id>/end", v.api_call_end, ["POST"]),
        ("/phone/numbers", v.api_phone_numbers, ["GET"]),
        ("/phone/numbers/<int:number_id>/settings", v.api_phone_number_settings, ["GET", "PUT"]),
        ("/phone/availability", v.api_phone_availability, ["GET", "PUT"]),
        ("/phone/receive-calls", v.api_phone_receive_calls, ["GET", "PUT"]),
        ("/phone/voice-client-error", v.api_phone_voice_client_error, ["POST"]),
        ("/pwa/badge-count", v.pwa_badge_count, ["GET"]),
        ("/pwa/preferences", v.api_pwa_preferences, ["GET", "PATCH"]),
        ("/pwa/notifications", v.pwa_notifications, ["GET"]),
        ("/pwa/notifications/read", v.pwa_notifications_read, ["POST"]),
    ]
    for rule, view, methods in routes:
        mobile_api_bp.add_url_rule(rule, endpoint="shared_" + view.__name__, view_func=view, methods=methods)


_mount_shared_views()
