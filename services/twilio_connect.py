"""Bring-your-own Twilio: a workspace connects its own Twilio account (T-32, D-6 option A).

connect(company, actor, account_sid, auth_token)
    Checks the credentials against Twilio (the account must exist, be active and
    be a full account, not the platform's own and not already connected to
    another workspace), stores them encrypted on the workspace's
    ``TwilioAccount`` and points signature checks at our public base URL.

available_numbers(company) / attach_number(company, number_sid)
    Lists the numbers in *their* Twilio account; attaching one proves it belongs
    to that account (looked up by SID through their credentials), refuses a
    number already registered to another workspace, sets its SMS / voice /
    status webhooks to LUXit and registers it for the workspace.

disconnect(company, actor)
    Detaches the workspace's numbers (webhooks we set are cleared on Twilio
    when the credentials still work), deactivates and wipes the stored
    credentials. Message history stays.

Sending and calling also need the phone/SMS license: connecting grants it to
a paying workspace; a free workspace can connect but stays blocked from
sending (402) until it upgrades. All Twilio REST calls go through
``_client()``, which tests replace.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

from extensions import db

SID_RE = re.compile(r"^AC[0-9a-f]{32}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
NUMBER_SID_RE = re.compile(r"^PN[0-9a-f]{32}$")

SMS_PATH = "/twilio/sms/inbound"
VOICE_PATH = "/twilio/voice/inbound"
STATUS_PATH = "/twilio/sms/status"


class ConnectError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _client(account_sid: str, auth_token: str):
    from twilio.rest import Client
    return Client(account_sid, auth_token)


def public_base_url() -> str:
    base = os.environ.get("LUXIT_TWILIO_WEBHOOK_BASE") or os.environ.get("APP_BASE_URL") or "https://luxit.app"
    return base.rstrip("/")


def _platform_account_sid():
    try:
        from services.provider_config import get_provider_config
        sid = get_provider_config("twilio", "platform", "account_sid")
    except Exception:
        sid = None
    return sid or os.environ.get("TWILIO_ACCOUNT_SID")


def _account(company_id):
    from models import TwilioAccount
    return TwilioAccount.query.filter_by(company_id=company_id).first()


def _connected_client(company):
    ta = _account(company.id)
    if not ta or not ta.is_active or not ta.get_account_sid() or not ta.get_auth_token():
        raise ConnectError("not_connected", "Connect your Twilio account first.", 409)
    return ta, _client(ta.get_account_sid(), ta.get_auth_token())


def status(company) -> dict:
    from models import TwilioPhoneNumber
    from services.license_service import PHONE_PWA_FEATURE, license_status_details
    ta = _account(company.id)
    sid = ta.get_account_sid() if ta else None
    numbers = (TwilioPhoneNumber.query.filter_by(company_id=company.id, is_active=True)
               .order_by(TwilioPhoneNumber.id).all())
    return {
        "connected": bool(ta and ta.is_active and sid),
        "account_sid_hint": f"AC…{sid[-4:]}" if sid else None,
        "numbers": [{"id": n.id, "phone_number": n.phone_number, "sms": bool(n.sms_enabled),
                     "voice": bool(n.voice_enabled)} for n in numbers],
        "license": {k: v for k, v in license_status_details(company.id, PHONE_PWA_FEATURE).items()
                    if k in ("allowed", "status", "warning")},
    }


def _grant_license_if_paying(company, actor):
    from services import entitlements
    from services.license_service import PHONE_PWA_FEATURE, _ensure_license, get_company_license, log_license_event
    if entitlements.is_free_plan(company.id):
        return "free_plan"
    existing = get_company_license(company.id, PHONE_PWA_FEATURE)
    if existing is not None:
        return existing.status
    lic = _ensure_license(company.id, PHONE_PWA_FEATURE)
    db.session.flush()
    log_license_event(company.id, lic, "license_granted", None, "active", {"reason": "byo_twilio_connected"},
                      getattr(actor, "id", None), "owner")
    return "active"


def connect(company, actor, account_sid: str, auth_token: str) -> dict:
    from models import TwilioAccount

    account_sid = (account_sid or "").strip()
    auth_token = (auth_token or "").strip()
    if not SID_RE.match(account_sid) or not TOKEN_RE.match(auth_token):
        raise ConnectError("invalid_format", "Enter the Account SID (AC…) and Auth Token from your Twilio console.")
    platform_sid = _platform_account_sid()
    if platform_sid and account_sid == platform_sid:
        raise ConnectError("platform_account", "That Twilio account can't be connected to a workspace.", 403)
    for other in TwilioAccount.query.filter(TwilioAccount.company_id != company.id).all():
        if other.get_account_sid() == account_sid and other.is_active:
            raise ConnectError("in_use", "That Twilio account is already connected to another workspace.", 409)
    try:
        account = _client(account_sid, auth_token).api.v2010.accounts(account_sid).fetch()
    except Exception as exc:  # twilio raises TwilioRestException on 401/404
        if getattr(exc, "status", None) in (401, 403, 404):
            raise ConnectError("rejected", "Twilio rejected those credentials.", 400) from None
        raise ConnectError("twilio_unavailable", "Couldn't reach Twilio. Try again.", 502) from None
    if (getattr(account, "status", "") or "").lower() != "active":
        raise ConnectError("account_inactive", f"That Twilio account is {account.status}.", 400)
    if (getattr(account, "type", "") or "").lower() == "trial":
        raise ConnectError("trial_account", "Upgrade the Twilio account from trial before connecting "
                                            "(trial accounts can only text verified numbers).", 400)

    ta = _account(company.id)
    if ta is None:
        ta = TwilioAccount(company_id=company.id)
        db.session.add(ta)
    ta.set_account_sid(account_sid)
    ta.set_auth_token(auth_token)
    ta.webhook_base_url = public_base_url()
    ta.is_active = True
    license_state = _grant_license_if_paying(company, actor)
    db.session.commit()
    return {"connected": True, "account_sid_hint": f"AC…{account_sid[-4:]}",
            "friendly_name": getattr(account, "friendly_name", None), "license": license_state}


def available_numbers(company) -> list[dict]:
    from models import TwilioPhoneNumber
    _, client = _connected_client(company)
    out = []
    for n in client.incoming_phone_numbers.list(limit=200):
        owner = TwilioPhoneNumber.query.filter_by(phone_number=n.phone_number).first()
        caps = getattr(n, "capabilities", None) or {}
        out.append({
            "sid": n.sid, "phone_number": n.phone_number, "friendly_name": n.friendly_name,
            "sms": bool(caps.get("sms")), "voice": bool(caps.get("voice")), "mms": bool(caps.get("mms")),
            "attached": bool(owner and owner.company_id == company.id and owner.is_active),
            "unavailable": bool(owner and owner.company_id != company.id),
        })
    return out


def attach_number(company, number_sid: str) -> dict:
    from models import TwilioPhoneNumber
    if not NUMBER_SID_RE.match(number_sid or ""):
        raise ConnectError("invalid_number", "Pick a number from your Twilio account.")
    ta, client = _connected_client(company)
    try:
        number = client.incoming_phone_numbers(number_sid).fetch()
    except Exception:
        raise ConnectError("not_in_account", "That number isn't in your Twilio account.", 404) from None
    existing = TwilioPhoneNumber.query.filter_by(phone_number=number.phone_number).first()
    if existing and existing.company_id != company.id:
        raise ConnectError("number_in_use", "That number is registered to another workspace.", 409)

    base = ta.webhook_base_url or public_base_url()
    urls = {"sms_url": base + SMS_PATH, "voice_url": base + VOICE_PATH, "status_callback": base + STATUS_PATH}
    try:
        client.incoming_phone_numbers(number_sid).update(sms_url=urls["sms_url"], sms_method="POST",
                                                         voice_url=urls["voice_url"], voice_method="POST",
                                                         status_callback=urls["status_callback"])
    except Exception:
        raise ConnectError("twilio_unavailable", "Couldn't set the number's webhooks on Twilio. Try again.", 502) from None

    caps = getattr(number, "capabilities", None) or {}
    pn = existing or TwilioPhoneNumber(company_id=company.id, phone_number=number.phone_number)
    if existing is None:
        db.session.add(pn)
    pn.twilio_account_id = ta.id
    pn.twilio_sid = number.sid
    pn.friendly_name = number.friendly_name or number.phone_number
    pn.sms_enabled = bool(caps.get("sms", True))
    pn.voice_enabled = bool(caps.get("voice", True))
    pn.mms_enabled = bool(caps.get("mms", False))
    pn.sms_webhook_url, pn.voice_webhook_url = urls["sms_url"], urls["voice_url"]
    pn.status_callback_webhook_url = urls["status_callback"]
    pn.allow_global_fallback = False
    pn.is_active = True
    if not TwilioPhoneNumber.query.filter_by(company_id=company.id, is_active=True, is_primary=True).first():
        pn.is_primary = True
    if not ta.from_phone:
        ta.from_phone = number.phone_number
    db.session.commit()
    return {"id": pn.id, "phone_number": pn.phone_number, "primary": bool(pn.is_primary)}


def disconnect(company, actor) -> dict:
    from models import TwilioPhoneNumber
    ta = _account(company.id)
    if ta is None or not ta.is_active:
        raise ConnectError("not_connected", "No Twilio account is connected.", 409)
    numbers = TwilioPhoneNumber.query.filter_by(company_id=company.id, is_active=True).all()
    cleared = 0
    try:
        client = _client(ta.get_account_sid(), ta.get_auth_token())
        for pn in numbers:
            if not pn.twilio_sid:
                continue
            try:
                live = client.incoming_phone_numbers(pn.twilio_sid).fetch()
                if (live.sms_url or "") == pn.sms_webhook_url or (live.voice_url or "") == pn.voice_webhook_url:
                    client.incoming_phone_numbers(pn.twilio_sid).update(sms_url="", voice_url="", status_callback="")
                    cleared += 1
            except Exception:
                continue
    except Exception:
        pass  # credentials already revoked on Twilio's side: nothing to clear
    for pn in numbers:
        pn.is_active = False
        pn.is_primary = False
    ta.is_active = False
    ta.set_account_sid("")
    ta.set_auth_token("")
    ta.from_phone = None
    ta.updated_at = datetime.utcnow()
    db.session.commit()
    return {"disconnected": True, "numbers_detached": len(numbers), "webhooks_cleared": cleared}
