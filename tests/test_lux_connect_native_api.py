"""LUX Connect native app API (/api/mobile/v1) — backend contract.

Covers: bearer-only auth (no cookie, no CSRF hole), refresh rotation with
reuse detection and the lost-response grace, sign-in throttling, revocation
(logout / remote revoke / archived user / removed membership / global
session revoke), tenant isolation through the shared PWA views, the native
Voice token, native handsets in the inbound <Dial>, native outbound identity
verification, the idempotent unified text/MMS send (STOP still enforced,
10DLC block), handset reassignment, native push decisions, sync and number
assignment.
"""
import hashlib
import os
import re
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "g2CDXwdc6VKAElQ5QWqFBCsmXL_dQAs3e44_Gl1oJaU=")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_API_KEY", "SKtest")
os.environ.setdefault("TWILIO_API_SECRET", "secretsecretsecret")

from app import create_app
from extensions import db
from models import (Company, MobileDevice, MobileMediaUpload, MobileSession, PhoneNumberMessagingRegistration,
                    PhoneNumberUserPermission, PWADevice, TwilioAccount, TwilioCallLog, TwilioConversation,
                    TwilioMessage, TwilioPhoneNumber, User, UserCompanyAccess)
from services import mobile_auth, native_push
from services.phone_availability import set_availability
from services.phone_identity import mobile_voice_identity, pwa_voice_identity
from services.receive_calls import set_receive_calls

NUMBER = "+19165550123"
OTHER_NUMBER = "+19165550999"
UID_A = "ios-install-aaaaaaaaaaaaaaaa"
UID_B = "android-install-bbbbbbbbbbbbbb"


@pytest.fixture
def app():
    a = create_app()
    a.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                    WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _tenant(name="T", number=NUMBER, require_approval=False):
    co = Company(name=name, is_active=True, require_approved_pwa_devices=require_approval)
    db.session.add(co); db.session.flush()
    ta = TwilioAccount(company_id=co.id, from_phone=number, is_active=True,
                       _account_sid="ACtest", _auth_token="auth")
    db.session.add(ta); db.session.flush()
    pn = TwilioPhoneNumber(company_id=co.id, twilio_account_id=ta.id, phone_number=number,
                           friendly_name="Main", is_active=True, voice_enabled=True,
                           during_hours_route="ring_pwa", after_hours_route="ring_pwa")
    db.session.add(pn); db.session.flush()
    return co, ta, pn


def _user(co, pn, name="rep", *, role="staff", can_call=True, permission=True, password="pw-123456"):
    u = User(username=name, email=f"{name}@t.co", password_hash=generate_password_hash(password),
             active=True, default_company_id=co.id)
    db.session.add(u); db.session.flush()
    db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role=role, is_active=True,
                                     pwa_access_enabled=True, can_access_full_app=True, is_default=True))
    if permission:
        db.session.add(PhoneNumberUserPermission(company_id=co.id, user_id=u.id, phone_number_id=pn.id,
                                                 can_access_pwa=True, can_call=can_call, can_view_calls=True,
                                                 can_view_sms=True, can_send_sms=True))
    db.session.flush()
    return u


def _login(client, name="rep", uid=UID_A, platform="ios", password="pw-123456"):
    return client.post("/api/mobile/v1/auth/login", json={
        "email": f"{name}@t.co", "password": password,
        "device": {"device_uid": uid, "platform": platform, "device_name": "Phone", "app_version": "1.0.0"},
    })


def _auth(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _signed_in(client, name="rep", uid=UID_A, platform="ios"):
    r = _login(client, name, uid, platform)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _inbound(client, monkeypatch, sid="CAin1", to=NUMBER):
    import twilio_sms
    monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda *a, **k: True)
    r = client.post("/twilio/voice/inbound", data={"To": to, "From": "+14155559999", "CallSid": sid,
                                                   "Direction": "inbound"})
    return r.get_data(as_text=True)


def _identities(twiml):
    return re.findall(r"<Identity>([^<]+)</Identity>", twiml)


def _register_voice(client, tokens, voice_token="voip-token-1"):
    r = client.post("/api/mobile/v1/devices/current/voice-registration", headers=_auth(tokens),
                    json={"registered": True, "voice_device_token": voice_token})
    assert r.status_code == 200
    return r


# ══ authentication ═════════════════════════════════════════════════════════ #

def test_login_issues_bearer_tokens_and_sets_no_cookie(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    r = _login(client)
    body = r.get_json()
    assert r.status_code == 200 and body["success"]
    assert body["access_token"].startswith("lxa_") and body["refresh_token"].startswith("lxr_")
    assert "Set-Cookie" not in r.headers
    row = MobileSession.query.one()
    assert body["access_token"] not in (row.access_token_hash, row.refresh_token_hash)  # only hashes stored
    assert row.access_token_hash == mobile_auth.token_hash(body["access_token"])
    me = client.get("/api/mobile/v1/me", headers=_auth(body))
    assert me.status_code == 200 and me.get_json()["company"]["id"] == co.id
    assert "Set-Cookie" not in me.headers


def test_wrong_password_and_throttle(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    for _ in range(mobile_auth.MAX_FAILURES_PER_IDENTIFIER):
        assert _login(client, password="wrong").status_code == 401
    r = _login(client)  # even the right password is throttled now
    assert r.status_code == 429 and r.get_json()["code"] == "too_many_attempts"


def test_cookie_session_is_not_accepted_on_mobile_api(app, client):
    co, _ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    with client.session_transaction() as s:
        s["_user_id"] = str(u.id); s["_fresh"] = True
    r = client.get("/api/mobile/v1/me")
    assert r.status_code == 401 and r.get_json()["code"] == "auth_required"
    # and a cookie-only POST cannot send a text through the CSRF-exempt prefix
    r = client.post("/api/mobile/v1/messages", json={"to": "+14155550000", "body": "x",
                                                     "idempotency_key": "k" * 20})
    assert r.status_code == 401


def test_bearer_post_works_with_csrf_enabled_and_web_still_requires_csrf(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    r = client.post("/api/mobile/v1/devices/current/voice-registration", headers=_auth(tokens),
                    json={"registered": False, "error": "test"})
    assert r.status_code == 200
    # The web PWA route is unchanged: no CSRF token -> rejected.
    with client.session_transaction() as s:
        s["_user_id"] = str(User.query.first().id); s["_fresh"] = True
    r = client.post("/api/calls/1/accept")
    assert r.status_code in (302, 400, 403)  # the app's CSRFError handler redirects
    assert r.status_code != 200


def test_refresh_rotates_and_invalidates_previous_tokens(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    first = _signed_in(client)
    r = client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    second = r.get_json()
    assert r.status_code == 200 and second["access_token"] != first["access_token"]
    assert client.get("/api/mobile/v1/me", headers=_auth(first)).status_code == 401
    assert client.get("/api/mobile/v1/me", headers=_auth(second)).status_code == 200


def test_refresh_reuse_after_successor_used_revokes_device(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    first = _signed_in(client)
    second = client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}).get_json()
    assert client.get("/api/mobile/v1/me", headers=_auth(second)).status_code == 200  # successor used
    r = client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401 and r.get_json()["code"] == "session_revoked"
    assert client.get("/api/mobile/v1/me", headers=_auth(second)).status_code == 401


def test_refresh_lost_response_grace(app, client):
    """The phone never received the refresh response and retries with the old token."""
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    first = _signed_in(client)
    lost = client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}).get_json()
    retry = client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert retry.status_code == 200
    assert client.get("/api/mobile/v1/me", headers=_auth(retry.get_json())).status_code == 200
    assert client.get("/api/mobile/v1/me", headers=_auth(lost)).status_code == 401


def test_refresh_reuse_outside_grace_revokes(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    first = _signed_in(client)
    client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    old = MobileSession.query.filter_by(refresh_token_hash=mobile_auth.token_hash(first["refresh_token"])).one()
    old.rotated_at = datetime.utcnow() - timedelta(minutes=5)
    db.session.commit()
    r = client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401


def test_expired_access_token_is_rejected_with_specific_code(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    MobileSession.query.update({"access_expires_at": datetime.utcnow() - timedelta(seconds=1)})
    db.session.commit()
    r = client.get("/api/mobile/v1/me", headers=_auth(tokens))
    assert r.status_code == 401 and r.get_json()["code"] == "token_expired"


@pytest.mark.parametrize("change", ["archive_user", "remove_membership", "revoke_all_sessions"])
def test_principal_changes_take_effect_on_next_request(app, client, change):
    co, _ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    if change == "archive_user":
        u.active = False
    elif change == "remove_membership":
        UserCompanyAccess.query.filter_by(user_id=u.id).update({"is_active": False})
    else:
        u.session_revoked_at = datetime.utcnow() + timedelta(seconds=1)
    db.session.commit()
    assert client.get("/api/mobile/v1/me", headers=_auth(tokens)).status_code == 401


def test_user_without_mobile_access_cannot_sign_in(app, client):
    co, _ta, pn = _tenant(); u = _user(co, pn)
    UserCompanyAccess.query.filter_by(user_id=u.id).update({"pwa_access_enabled": False, "role": "viewer"})
    db.session.commit()
    r = _login(client)
    assert r.status_code == 403 and r.get_json()["code"] == "mobile_access_disabled"


# ══ tenant isolation ═══════════════════════════════════════════════════════ #

def test_other_tenant_conversation_and_device_are_invisible(app, client):
    co_a, _ta, pn_a = _tenant("A"); _user(co_a, pn_a, "alice")
    co_b, _tb, pn_b = _tenant("B", OTHER_NUMBER); bob = _user(co_b, pn_b, "bob")
    conv_b = TwilioConversation(company_id=co_b.id, from_number="+14155550001", to_number=OTHER_NUMBER,
                                phone_number_id=pn_b.id)
    db.session.add(conv_b); db.session.commit()
    bob_tokens = _signed_in(client, "bob", UID_B, "android")
    alice = _signed_in(client, "alice")
    assert client.get(f"/api/mobile/v1/inbox/conversations/{conv_b.id}", headers=_auth(alice)).status_code == 404
    listed = client.get("/api/mobile/v1/inbox/conversations", headers=_auth(alice)).get_json()
    assert all(c["id"] != conv_b.id for c in listed.get("conversations", []))
    bob_device = MobileDevice.query.filter_by(user_id=bob.id).one()
    assert client.delete(f"/api/mobile/v1/devices/{bob_device.id}", headers=_auth(alice)).status_code == 404
    r = client.post("/api/mobile/v1/messages", headers=_auth(alice),
                    json={"conversation_id": conv_b.id, "body": "hi", "idempotency_key": "x" * 20})
    assert r.status_code == 404
    assert client.get("/api/mobile/v1/me", headers=_auth(bob_tokens)).status_code == 200


# ══ voice ══════════════════════════════════════════════════════════════════ #

def test_voice_token_is_device_scoped_with_platform_push_credential(app, client, monkeypatch):
    monkeypatch.setenv("TWILIO_PUSH_CREDENTIAL_SID_IOS", "CRios")
    monkeypatch.setenv("TWILIO_PUSH_CREDENTIAL_SID_ANDROID", "CRandroid")
    monkeypatch.setenv("TWILIO_TWIML_APP_SID", "APapp")
    co, _ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    body = client.get("/api/mobile/v1/voice/token", headers=_auth(tokens)).get_json()
    device = MobileDevice.query.one()
    assert body["identity"] == mobile_voice_identity(co.id, u.id, device.id, UID_A)
    assert body["identity"] != pwa_voice_identity(co.id, u.id)
    assert body["incoming_enabled"] and body["outbound_enabled"]
    import jwt
    claims = jwt.decode(body["token"], options={"verify_signature": False})
    assert claims["grants"]["voice"]["push_credential_sid"] == "CRios"
    assert claims["grants"]["identity"] == body["identity"]


def test_voice_token_without_push_credential_is_honest(app, client, monkeypatch):
    monkeypatch.delenv("TWILIO_PUSH_CREDENTIAL_SID_IOS", raising=False)
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    body = client.get("/api/mobile/v1/voice/token", headers=_auth(tokens)).get_json()
    assert body["incoming_enabled"] is False
    me = client.get("/api/mobile/v1/me", headers=_auth(tokens)).get_json()
    assert me["calling"]["incoming"] is False and me["calling"]["reason"] == "PUSH_CREDENTIAL_NOT_CONFIGURED"


def test_voice_token_uses_the_shared_gate(app, client):
    co, _ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    set_receive_calls(u.id, co.id, False, actor_user_id=u.id, source="user"); db.session.commit()
    r = client.get("/api/mobile/v1/voice/token", headers=_auth(tokens))
    assert r.status_code == 403 and r.get_json()["code"] == "CALLING_DISABLED"


def test_pending_approval_device_gets_no_token_until_approved(app, client):
    co, _ta, pn = _tenant(require_approval=True)
    _user(co, pn)
    _user(co, pn, "boss", role="owner")
    db.session.commit()
    tokens = _signed_in(client)
    r = client.get("/api/mobile/v1/voice/token", headers=_auth(tokens))
    assert r.status_code == 403 and r.get_json()["code"] == "DEVICE_PENDING_APPROVAL"
    boss = _signed_in(client, "boss", UID_B, "android")
    device = MobileDevice.query.filter_by(device_uid=UID_A).one()
    assert client.post(f"/api/mobile/v1/devices/{device.id}/approve", headers=_auth(boss)).status_code == 200
    assert client.get("/api/mobile/v1/voice/token", headers=_auth(tokens)).status_code == 200


def test_inbound_rings_pwa_and_every_registered_native_handset(app, client, monkeypatch):
    co, _ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    iphone = _signed_in(client, uid=UID_A, platform="ios")
    android = _signed_in(client, uid=UID_B, platform="android")
    _register_voice(client, iphone, "voip-ios"); _register_voice(client, android, "fcm-android")
    ids = _identities(_inbound(client, monkeypatch))
    d_ios = MobileDevice.query.filter_by(device_uid=UID_A).one()
    d_and = MobileDevice.query.filter_by(device_uid=UID_B).one()
    assert pwa_voice_identity(co.id, u.id) in ids
    assert mobile_voice_identity(co.id, u.id, d_ios.id, UID_A) in ids
    assert mobile_voice_identity(co.id, u.id, d_and.id, UID_B) in ids
    assert len(ids) == len(set(ids)) == 3  # one <Dial>, no duplicates


@pytest.mark.parametrize("exclusion", ["not_registered", "ring_disabled", "signed_out", "revoked",
                                       "sessions_revoked", "sessions_expired", "password_reset",
                                       "away", "no_number_permission"])
def test_inbound_excludes_ineligible_handsets(app, client, monkeypatch, exclusion):
    co, _ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    if exclusion != "not_registered":
        _register_voice(client, tokens)
    device = MobileDevice.query.one()
    if exclusion == "ring_disabled":
        client.patch(f"/api/mobile/v1/devices/{device.id}", headers=_auth(tokens), json={"ring_enabled": False})
    elif exclusion == "signed_out":
        client.post("/api/mobile/v1/auth/logout", headers=_auth(tokens))
    elif exclusion == "revoked":
        client.delete(f"/api/mobile/v1/devices/{device.id}", headers=_auth(tokens))
    elif exclusion == "sessions_revoked":
        # Bulk revoke (admin / ops) leaves the device row active and registered.
        from services.mobile_auth import revoke_device_sessions
        revoke_device_sessions(device, "test"); db.session.commit()
    elif exclusion == "sessions_expired":
        # Refresh lapsed without a sign-out reaching the server.
        MobileSession.query.update({"refresh_expires_at": datetime.utcnow() - timedelta(minutes=1)}); db.session.commit()
    elif exclusion == "password_reset":
        # Reset/change stamps the account, not each MobileSession row.
        u.session_revoked_at = datetime.utcnow(); db.session.commit()
    elif exclusion == "away":
        set_availability(u.id, co.id, "away", actor_user_id=u.id, source="user"); db.session.commit()
    elif exclusion == "no_number_permission":
        PhoneNumberUserPermission.query.filter_by(user_id=u.id).update({"can_call": False}); db.session.commit()
    from flask import g
    for key in list(vars(g)):  # shared app context: don't let a test login redirect the webhook
        g.pop(key, None)
    twiml = _inbound(client, monkeypatch)
    assert twiml.lstrip().startswith("<?xml"), twiml[:120]
    assert mobile_voice_identity(co.id, u.id, device.id, UID_A) not in _identities(twiml)


def test_logout_revokes_sessions_and_push(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    client.put("/api/mobile/v1/devices/current/push-token", headers=_auth(tokens),
               json={"token": "a" * 64, "type": "apns"})
    assert client.post("/api/mobile/v1/auth/logout", headers=_auth(tokens)).status_code == 200
    device = MobileDevice.query.one()
    assert device.status == "signed_out" and device.push_token is None and device.voice_registered_at is None
    assert client.get("/api/mobile/v1/me", headers=_auth(tokens)).status_code == 401
    assert client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_handset_reassigned_to_another_user_stops_ringing_previous_user(app, client, monkeypatch):
    co, _ta, pn = _tenant(); a = _user(co, pn, "anna"); _user(co, pn, "ben"); db.session.commit()
    anna = _signed_in(client, "anna", UID_A)
    _register_voice(client, anna, "same-handset-voip")
    ben = _signed_in(client, "ben", "ios-reinstall-cccccccccccccc")
    _register_voice(client, ben, "same-handset-voip")
    anna_device = MobileDevice.query.filter_by(user_id=a.id).one()
    assert anna_device.status == "signed_out"
    ids = _identities(_inbound(client, monkeypatch))
    assert mobile_voice_identity(co.id, a.id, anna_device.id, UID_A) not in ids
    assert client.get("/api/mobile/v1/me", headers=_auth(anna)).status_code == 401


def test_native_outbound_identity_verified_and_caller_id_is_assigned_number(app, client):
    co, _ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    _signed_in(client)
    device = MobileDevice.query.one()
    identity = mobile_voice_identity(co.id, u.id, device.id, UID_A)
    data = {"CallSid": "CAout1", "From": f"client:{identity}", "To": "(415) 555-0100"}
    twiml = client.post("/twilio/voice/pwa-outbound", data=data).get_data(as_text=True)
    assert f'callerId="{NUMBER}"' in twiml and "<Number>+14155550100</Number>" in twiml
    client.post("/twilio/voice/pwa-outbound", data=data)  # Twilio retry
    assert TwilioCallLog.query.filter_by(twilio_sid="CAout1").count() == 1


@pytest.mark.parametrize("tamper", ["digest", "revoked", "other_user"])
def test_native_outbound_rejects_unverified_identity(app, client, tamper):
    co, _ta, pn = _tenant(); u = _user(co, pn); other = _user(co, pn, "other"); db.session.commit()
    _signed_in(client)
    device = MobileDevice.query.one()
    identity = mobile_voice_identity(co.id, u.id, device.id, UID_A)
    if tamper == "digest":
        identity = identity[:-1] + ("0" if identity[-1] != "0" else "1")
    elif tamper == "revoked":
        device.status = "revoked"; db.session.commit()
    else:
        identity = identity.replace(f"_u{u.id}_", f"_u{other.id}_")
    twiml = client.post("/twilio/voice/pwa-outbound", data={
        "CallSid": "CAbad", "From": f"client:{identity}", "To": "+14155550100"}).get_data(as_text=True)
    assert "<Dial" not in twiml and "could not be authorized" in twiml


# ══ messaging ══════════════════════════════════════════════════════════════ #

def _fake_send(calls):
    def fake(conversation_id, message, **kw):
        calls.append({"conversation_id": conversation_id, "body": message, **kw})
        sid = f"SM{len(calls):030d}"
        conv = db.session.get(TwilioConversation, conversation_id)
        db.session.add(TwilioMessage(conversation_id=conversation_id, company_id=conv.company_id, twilio_sid=sid,
                                     direction="outbound", body=message, status="queued",
                                     media_urls=kw.get("media_urls")))
        db.session.commit()
        return {"success": True, "sid": sid, "status": "sent"}
    return fake


def test_send_requires_idempotency_key(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    r = client.post("/api/mobile/v1/messages", headers=_auth(tokens), json={"to": "+14155550000", "body": "hi"})
    assert r.status_code == 400 and r.get_json()["code"] == "idempotency_key_required"


def test_send_to_new_number_is_idempotent_across_retries(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    calls = []
    payload = {"to": "(415) 555-0000", "body": "On my way", "idempotency_key": "compose-0001-abcdef"}
    with patch("twilio_sms.sendConversationSms", side_effect=_fake_send(calls)):
        first = client.post("/api/mobile/v1/messages", headers=_auth(tokens), json=payload)
        retry = client.post("/api/mobile/v1/messages", headers=_auth(tokens), json=payload)
    assert first.status_code == 200 and retry.status_code == 200
    assert len(calls) == 1
    assert retry.get_json()["idempotent"] is True
    assert first.get_json()["conversation_id"] == retry.get_json()["conversation_id"]
    conv = db.session.get(TwilioConversation, first.get_json()["conversation_id"])
    assert conv.from_number == "+14155550000" and conv.to_number == NUMBER
    assert conv.message_count == 1


def test_send_to_stopped_contact_is_blocked(app, client):
    co, _ta, pn = _tenant(); _user(co, pn)
    conv = TwilioConversation(company_id=co.id, from_number="+14155550000", to_number=NUMBER,
                              phone_number_id=pn.id, is_opted_out=True)
    db.session.add(conv); db.session.commit()
    tokens = _signed_in(client)
    with patch("twilio_sms._build_client") as build:
        r = client.post("/api/mobile/v1/messages", headers=_auth(tokens),
                        json={"conversation_id": conv.id, "body": "hello", "idempotency_key": "stop-check-000001"})
    assert r.status_code == 409 and r.get_json()["code"] == "recipient_opted_out"
    build.assert_not_called()


def test_mms_upload_send_and_media_urls(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    import io
    up = client.post("/api/mobile/v1/media", headers=_auth(tokens), content_type="multipart/form-data",
                     data={"file": (io.BytesIO(b"\x89PNG fake"), "photo.png", "image/png")})
    assert up.status_code == 200
    media_id = up.get_json()["media"]["id"]
    calls = []
    with patch("twilio_sms.sendConversationSms", side_effect=_fake_send(calls)):
        r = client.post("/api/mobile/v1/messages", headers=_auth(tokens),
                        json={"to": "+14155550000", "media_ids": [media_id], "idempotency_key": "mms-0001-abcdefgh"})
    assert r.status_code == 200
    urls = calls[0]["media_urls"]
    assert len(urls) == 1 and "/mobile-media/" in urls[0]
    token = urls[0].rsplit("/", 1)[1]
    public = client.get(f"/mobile-media/{token}")
    assert public.status_code == 200 and public.data == b"\x89PNG fake"
    # The message as the app/PWA reads it points at the authenticated route, not the public one.
    shown = r.get_json()["message"]["media_urls"][0]
    assert shown == f"/api/inbox/media-uploads/{token}"
    assert client.get(f"/api/mobile/v1/inbox/media-uploads/{token}", headers=_auth(tokens)).status_code == 200
    MobileMediaUpload.query.update({"created_at": datetime.utcnow() - timedelta(hours=5)}); db.session.commit()
    assert client.get(f"/mobile-media/{token}").status_code == 404


def test_media_rejects_unsupported_and_oversize(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    import io
    r = client.post("/api/mobile/v1/media", headers=_auth(tokens), content_type="multipart/form-data",
                    data={"file": (io.BytesIO(b"MZ"), "x.exe", "application/x-msdownload")})
    assert r.status_code == 415
    big = io.BytesIO(b"0" * (5 * 1024 * 1024 + 1))
    r = client.post("/api/mobile/v1/media", headers=_auth(tokens), content_type="multipart/form-data",
                    data={"file": (big, "big.jpg", "image/jpeg")})
    assert r.status_code == 413


def test_unregistered_number_cannot_text_but_unknown_still_can(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    calls = []
    with patch("twilio_sms.sendConversationSms", side_effect=_fake_send(calls)):
        ok = client.post("/api/mobile/v1/messages", headers=_auth(tokens),
                         json={"to": "+14155550000", "body": "a", "idempotency_key": "unknown-reg-000001"})
        assert ok.status_code == 200
        db.session.add(PhoneNumberMessagingRegistration(company_id=co.id, phone_number_id=pn.id,
                                                        status="not_registered"))
        db.session.commit()
        r = client.post("/api/mobile/v1/messages", headers=_auth(tokens),
                        json={"to": "+14155550000", "body": "b", "idempotency_key": "not-reg-000000001"})
    assert r.status_code == 422 and r.get_json()["code"] == "messaging_not_registered"
    assert len(calls) == 1


def test_sync_returns_changes_since(app, client):
    co, _ta, pn = _tenant(); _user(co, pn)
    old = TwilioConversation(company_id=co.id, from_number="+14155550001", to_number=NUMBER, phone_number_id=pn.id,
                             last_message_at=datetime.utcnow() - timedelta(days=2))
    new = TwilioConversation(company_id=co.id, from_number="+14155550002", to_number=NUMBER, phone_number_id=pn.id,
                             last_message_at=datetime.utcnow())
    db.session.add_all([old, new]); db.session.commit()
    tokens = _signed_in(client)
    since = (datetime.utcnow() - timedelta(hours=1)).isoformat() + "Z"
    body = client.get(f"/api/mobile/v1/sync?since={since}", headers=_auth(tokens)).get_json()
    ids = [c["id"] for c in body["conversations"]]
    assert new.id in ids and old.id not in ids and body["server_time"].endswith("Z")


# ══ native push ════════════════════════════════════════════════════════════ #

def test_native_push_skips_calls_silent_and_unconfigured(app, client, monkeypatch):
    co, _ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    client.put("/api/mobile/v1/devices/current/push-token", headers=_auth(tokens),
               json={"token": "b" * 64, "type": "apns"})
    common = dict(title="t", body="b", link="/app/inbox?conv=5", badge=2, phone_number_id=pn.id)
    assert native_push.queue_for_user(co.id, u.id, event_type="incoming_sms", silent=False, **common) == 0  # nothing configured
    monkeypatch.setattr(native_push, "status", lambda: {"apns": True, "fcm": True})
    monkeypatch.setattr(native_push, "SYNC_FOR_TESTS", True)
    sent = []
    monkeypatch.setattr(native_push, "deliver", lambda job, devices: sent.append((job, devices)) or
                        {"sent": len(devices), "dead_ids": [devices[0]["id"]], "errors": 0})
    assert native_push.queue_for_user(co.id, u.id, event_type="incoming_call", silent=False, **common) == 0
    assert native_push.queue_for_user(co.id, u.id, event_type="incoming_sms", silent=True, **common) == 0
    assert native_push.queue_for_user(co.id, u.id, event_type="incoming_sms", silent=False, **common) == 1
    assert sent[0][1][0]["push_token_type"] == "apns"
    assert MobileDevice.query.one().push_token is None  # provider said the token is dead


def test_push_payloads_deep_link_and_badge():
    apns, fcm = native_push.build_messages({"title": "Ann", "body": "Hi", "link": "/app/inbox?conv=42",
                                            "event_type": "incoming_sms", "badge": 3, "phone_number_id": 7})
    assert apns["aps"]["badge"] == 3 and apns["route"] == "conversation" and apns["conversation_id"] == "42"
    assert fcm["android"]["notification"]["channel_id"] == "messages"
    assert fcm["data"]["conversation_id"] == "42" and all(isinstance(v, str) for v in fcm["data"].values())
    assert "notification" in fcm  # notification message: shown by the OS even with Twilio owning FCM


@pytest.mark.parametrize("event_type,link,route", [
    ("voicemail", "/app/inbox?tab=voicemail", "voicemail"),
    ("missed_call", "/app/inbox?tab=calls", "recents"),
])
def test_call_alert_payloads_route_and_use_calls_channel(event_type, link, route):
    # The exact links twilio_sms.py queues for voicemail / missed-call alerts.
    apns, fcm = native_push.build_messages({"title": "Voicemail", "body": "From Ann", "link": link,
                                            "event_type": event_type, "badge": 1, "phone_number_id": 7})
    assert apns["route"] == route and fcm["data"]["route"] == route
    assert fcm["android"]["notification"]["channel_id"] == "calls"
    assert "conversation_id" not in apns


def test_remote_revoke_of_another_device_kills_it_everywhere(app, client, monkeypatch):
    """Lost phone: revoked from the user's other handset. It cannot refresh, get a
    voice token, ring, or place a call; the revoking handset keeps working."""
    co, _ta, pn = _tenant(); u = _user(co, pn); _user(co, pn, "colleague"); db.session.commit()
    iphone = _signed_in(client, uid=UID_A, platform="ios")
    lost = _signed_in(client, uid=UID_B, platform="android")
    _register_voice(client, iphone, "voip-ios"); _register_voice(client, lost, "fcm-lost")
    d_lost = MobileDevice.query.filter_by(device_uid=UID_B).one()
    lost_identity = mobile_voice_identity(co.id, u.id, d_lost.id, UID_B)

    # A colleague (non-admin) in the same tenant cannot see or revoke it.
    colleague = _signed_in(client, "colleague", uid="ios-colleague-cccccccccccc")
    assert client.delete(f"/api/mobile/v1/devices/{d_lost.id}", headers=_auth(colleague)).status_code == 404

    assert client.delete(f"/api/mobile/v1/devices/{d_lost.id}", headers=_auth(iphone)).status_code == 200
    assert client.get("/api/mobile/v1/me", headers=_auth(lost)).status_code == 401
    assert client.get("/api/mobile/v1/voice/token", headers=_auth(lost)).status_code == 401
    assert client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": lost["refresh_token"]}).status_code == 401

    ids = _identities(_inbound(client, monkeypatch))
    assert lost_identity not in ids
    assert mobile_voice_identity(co.id, u.id, MobileDevice.query.filter_by(device_uid=UID_A).one().id, UID_A) in ids

    twiml = client.post("/twilio/voice/pwa-outbound", data={
        "CallSid": "CAlost", "From": f"client:{lost_identity}", "To": "+14155550100"}).get_data(as_text=True)
    assert "<Dial" not in twiml
    assert client.get("/api/mobile/v1/me", headers=_auth(iphone)).status_code == 200


def test_push_token_type_must_match_platform(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client, platform="ios")
    r = client.put("/api/mobile/v1/devices/current/push-token", headers=_auth(tokens),
                   json={"token": "fcm:token", "type": "fcm"})
    assert r.status_code == 400


# ══ numbers ════════════════════════════════════════════════════════════════ #

def test_number_assignment_admin_only_and_tenant_scoped(app, client):
    co, _ta, pn = _tenant(); _user(co, pn, "staffer"); owner = _user(co, pn, "owner", role="owner")
    newbie = _user(co, pn, "newbie", permission=False)
    co_b, _tb, pn_b = _tenant("B", OTHER_NUMBER); outsider = _user(co_b, pn_b, "outsider")
    db.session.commit()
    staff = _signed_in(client, "staffer")
    assert client.get("/api/mobile/v1/admin/numbers", headers=_auth(staff)).status_code == 403
    boss = _signed_in(client, "owner", UID_B, "android")
    listed = client.get("/api/mobile/v1/admin/numbers", headers=_auth(boss)).get_json()["numbers"]
    assert [n["phone_number"] for n in listed] == [NUMBER]
    r = client.put(f"/api/mobile/v1/admin/numbers/{pn.id}/assignments/{newbie.id}", headers=_auth(boss), json={})
    assert r.status_code == 200 and r.get_json()["assignment"]["can_call"] is True
    r = client.put(f"/api/mobile/v1/admin/numbers/{pn.id}/assignments/{outsider.id}", headers=_auth(boss), json={})
    assert r.status_code == 404
    r = client.put(f"/api/mobile/v1/admin/numbers/{pn_b.id}/assignments/{newbie.id}", headers=_auth(boss), json={})
    assert r.status_code == 404
    assert client.delete(f"/api/mobile/v1/admin/numbers/{pn.id}", headers=_auth(boss)).status_code == 405
    assert owner.id


def test_me_reports_numbers_and_messaging_status(app, client):
    co, _ta, pn = _tenant(); _user(co, pn)
    db.session.add(PhoneNumberMessagingRegistration(company_id=co.id, phone_number_id=pn.id, status="registered"))
    db.session.commit()
    tokens = _signed_in(client)
    me = client.get("/api/mobile/v1/me", headers=_auth(tokens)).get_json()
    assert me["numbers"][0]["phone_number"] == NUMBER
    assert me["numbers"][0]["messaging"]["status"] == "registered"
    assert me["device"]["is_current"] is True


def test_pwa_device_rows_are_untouched_by_native_sign_in(app, client):
    co, _ta, pn = _tenant(); u = _user(co, pn)
    db.session.add(PWADevice(company_id=co.id, user_id=u.id, device_key="dk", approved_status="approved",
                             lifecycle_status="active"))
    db.session.commit()
    _signed_in(client)
    assert PWADevice.query.count() == 1 and MobileDevice.query.count() == 1


# ══ LUXit Phone features: number settings, call routing, rules, sender ════ #

def _admin(co, pn, name="boss"):
    return _user(co, pn, name, role="admin")


def _rule(client, tokens, **body):
    return client.post("/api/mobile/v1/automation/rules", headers=_auth(tokens), json=body)


def test_number_settings_forwarding_admin_only_validated_and_tenant_scoped(app, client):
    co, _ta, pn = _tenant(); _admin(co, pn); _user(co, pn); db.session.commit()
    co2, _ta2, pn2 = _tenant("Other", OTHER_NUMBER); _user(co2, pn2, "outsider"); db.session.commit()
    boss, rep = _signed_in(client, "boss"), _signed_in(client, "rep", UID_B, "android")
    url = f"/api/mobile/v1/phone/numbers/{pn.id}/settings"

    r = client.put(url, headers=_auth(boss), json={"sms_forward_to": "(415) 555-0100", "sms_forwarding_enabled": True})
    assert r.status_code == 200 and r.get_json()["settings"]["sms_forward_to"] == "+14155550100"
    assert client.put(url, headers=_auth(boss), json={"sms_forward_to": "12"}).status_code == 400
    assert client.put(url, headers=_auth(boss), json={"sms_forward_to": NUMBER}).status_code == 400  # loop
    assert client.put(url, headers=_auth(boss), json={"call_forwarding_number": NUMBER}).status_code == 400
    r = client.put(url, headers=_auth(boss), json={"sms_forward_to": ""})
    assert r.get_json()["settings"]["sms_forward_to"] is None

    got = client.get(url, headers=_auth(rep)).get_json()
    assert got["editable"] is False
    assert client.put(url, headers=_auth(rep), json={"sms_forwarding_enabled": False}).status_code == 403
    outsider = _signed_in(client, "outsider", "ios-outsider-xxxxxxxxxxxx")
    assert client.get(url, headers=_auth(outsider)).status_code == 404


def test_call_routing_status_matches_webhook_and_masks_destination_for_staff(app, client, monkeypatch):
    co, _ta, pn = _tenant(); _admin(co, pn); _user(co, pn); db.session.commit()
    boss, rep = _signed_in(client, "boss"), _signed_in(client, "rep", UID_B, "android")
    me = client.get("/api/mobile/v1/me", headers=_auth(boss)).get_json()
    assert me["can_edit_phone_settings"] is True
    assert me["numbers"][0]["call_routing"]["business_hours"]["destination"] == "app"

    client.put(f"/api/mobile/v1/phone/numbers/{pn.id}/settings", headers=_auth(boss),
               json={"during_hours_route": "forward", "call_forwarding_number": "+14155550123", "call_forwarding_enabled": True})
    routing = client.get(f"/api/mobile/v1/phone/numbers/{pn.id}/routing", headers=_auth(boss)).get_json()["routing"]
    assert routing["business_hours"] == {"destination": "forward", "forward_to": "+14155550123"}
    staff = client.get(f"/api/mobile/v1/phone/numbers/{pn.id}/routing", headers=_auth(rep)).get_json()["routing"]
    assert staff["business_hours"] == {"destination": "forward", "forward_to": "•••0123"}
    assert client.get("/api/mobile/v1/me", headers=_auth(rep)).get_json()["can_edit_phone_settings"] is False

    # The status is the webhook's own decision: a business-hours call really is forwarded.
    twiml = _inbound(client, monkeypatch, sid="CAfwd")
    # The forwarded leg is screened ("press 1", services/call_forwarding.py).
    assert 'method="POST">+14155550123</Number>' in twiml and "<Client>" not in twiml


def test_rules_crud_admin_only_validation_and_isolation(app, client):
    co, _ta, pn = _tenant(); _admin(co, pn); _user(co, pn); db.session.commit()
    co2, _ta2, pn2 = _tenant("Other", OTHER_NUMBER); _admin(co2, pn2, "boss2"); db.session.commit()
    boss, rep = _signed_in(client, "boss"), _signed_in(client, "rep", UID_B, "android")
    boss2 = _signed_in(client, "boss2", "ios-boss2-yyyyyyyyyyyyyyy")

    r = _rule(client, boss, name="Hours", trigger_type="keyword_contains", keywords=["hours"], action="reply",
              response="We're open 9-5.", phone_number_id=pn.id)
    assert r.status_code == 201
    rid = r.get_json()["rule"]["id"]
    fwd = _rule(client, boss, name="Urgent", trigger_type="keyword_contains", keywords="urgent, asap",
                action="forward", forward_to="415-555-0100")
    assert fwd.status_code == 201 and fwd.get_json()["rule"]["forward_to"] == "+14155550100"
    assert fwd.get_json()["rule"]["keywords"] == ["urgent", "asap"]

    for bad, code in [
        (dict(name="x", trigger_type="keyword_contains", keywords=[], action="reply", response="r"), 400),
        (dict(name="x", trigger_type="always", action="reply", response=""), 400),
        (dict(name="x", trigger_type="always", action="forward", forward_to="nope"), 400),
        (dict(name="x", trigger_type="always", action="forward", forward_to=NUMBER), 400),  # own number
        (dict(name="x", trigger_type="always", action="tag"), 400),
        (dict(name="x", trigger_type="regex", keywords=["("], action="reply", response="r"), 400),
        (dict(name="x", trigger_type="stop_keyword", action="reply", response="r"), 400),
        (dict(name="x", trigger_type="always", action="reply", response="r", active_hours_start="09:00"), 400),
        (dict(name="x", trigger_type="keyword_contains", keywords=["HOURS"], action="reply", response="r"), 409),
        (dict(name="x", trigger_type="always", action="reply", response="r", phone_number_id=pn2.id), 404),
    ]:
        assert _rule(client, boss, **bad).status_code == code, bad

    listing = client.get("/api/mobile/v1/automation/rules", headers=_auth(rep)).get_json()
    assert listing["editable"] is False and {x["name"] for x in listing["rules"]} == {"Hours", "Urgent"}
    assert _rule(client, rep, name="x", trigger_type="always", action="reply", response="r").status_code == 403
    assert client.patch(f"/api/mobile/v1/automation/rules/{rid}", headers=_auth(rep), json={"is_active": False}).status_code == 403
    assert client.get(f"/api/mobile/v1/automation/rules/{rid}", headers=_auth(boss2)).status_code == 404
    assert client.get("/api/mobile/v1/automation/rules", headers=_auth(boss2)).get_json()["rules"] == []

    r = client.patch(f"/api/mobile/v1/automation/rules/{rid}", headers=_auth(boss), json={"response": "Open 8-6.", "is_active": False})
    assert r.status_code == 200 and r.get_json()["rule"]["response"] == "Open 8-6." and r.get_json()["rule"]["is_active"] is False
    assert r.get_json()["rule"]["keywords"] == ["hours"]  # partial update keeps the rest
    assert client.delete(f"/api/mobile/v1/automation/rules/{rid}", headers=_auth(boss)).status_code == 200
    assert client.get(f"/api/mobile/v1/automation/rules/{rid}", headers=_auth(boss)).status_code == 404


def test_system_stop_rule_cannot_be_changed_from_the_app(app, client):
    from models import AutoReplyRule
    co, _ta, pn = _tenant(); _admin(co, pn); db.session.commit()
    stop = AutoReplyRule(company_id=co.id, name="STOP", trigger_type="stop_keyword", action="opt_out", is_active=True)
    db.session.add(stop); db.session.commit()
    boss = _signed_in(client, "boss")
    listed = client.get("/api/mobile/v1/automation/rules", headers=_auth(boss)).get_json()["rules"]
    assert listed[0]["system"] is True
    assert client.patch(f"/api/mobile/v1/automation/rules/{stop.id}", headers=_auth(boss), json={"is_active": False}).status_code == 409
    assert client.delete(f"/api/mobile/v1/automation/rules/{stop.id}", headers=_auth(boss)).status_code == 409


def _inbound_sms(client, monkeypatch, body, sid, frm="+14155559999"):
    monkeypatch.setattr("services.contact_identity.apply_cached_google_match", lambda contact: "unmatched")
    monkeypatch.setattr("services.contact_identity.process_identity_message", lambda *a, **k: {"reply": None})
    monkeypatch.setattr("services.contact_identity.should_request_identity", lambda contact: False)
    return client.post("/twilio/sms/inbound", data={"From": frm, "To": NUMBER, "Body": body, "MessageSid": sid})


def _capture_sends(monkeypatch):
    sent = []

    def fake(conversation_id, message, **kw):  # mocked Twilio delivery: nothing leaves the process
        sent.append({"conversation_id": conversation_id, "body": message, **kw})
        return {"success": True, "sid": f"SMfake{len(sent)}"}
    monkeypatch.setattr("twilio_sms.sendConversationSms", fake)
    return sent


def test_forward_rule_sends_copy_to_destination(app, client, monkeypatch):
    from models import AutoReplyRule
    co, ta, pn = _tenant(); ta.automation_enabled = True
    db.session.add(AutoReplyRule(company_id=co.id, name="Urgent", trigger_type="keyword_contains", keywords=["urgent"],
                                 action="forward", forward_to="+14155550100", is_active=True))
    db.session.commit()
    sent = _capture_sends(monkeypatch)
    assert _inbound_sms(client, monkeypatch, "This is URGENT please call", "SMfwd1").status_code == 200
    fwd = [s for s in sent if s.get("to_number") == "+14155550100"]
    assert len(fwd) == 1
    assert fwd[0]["effect_type"].startswith("rule_forward:") and "This is URGENT please call" in fwd[0]["body"]
    assert "+14155559999" in fwd[0]["body"] and "Reply using" not in fwd[0]["body"]  # replies aren't relayed from rule numbers
    assert AutoReplyRule.query.one().match_count == 1


def test_forward_rule_not_duplicated_with_number_forwarding_and_skips_stop(app, client, monkeypatch):
    from models import AutoReplyRule
    co, ta, pn = _tenant(); ta.automation_enabled = True
    pn.sms_forward_to = "+14155550100"; pn.sms_forwarding_enabled = True
    db.session.add(AutoReplyRule(company_id=co.id, name="All", trigger_type="always", action="forward",
                                 forward_to="+14155550100", is_active=True))
    db.session.commit()
    sent = _capture_sends(monkeypatch)
    _inbound_sms(client, monkeypatch, "hello", "SMdup1")
    to_fwd = [s for s in sent if s.get("to_number") == "+14155550100"]
    assert len(to_fwd) == 1 and to_fwd[0]["effect_type"] == "inbound_forward"  # number forwarding only

    sent.clear()
    _inbound_sms(client, monkeypatch, "STOP", "SMstop1")
    assert sent == []  # STOP is answered by the system; no forward, no rule


def test_new_text_from_chosen_number_only_if_permitted(app, client, monkeypatch):
    co, ta, pn = _tenant(); u = _user(co, pn)
    second = TwilioPhoneNumber(company_id=co.id, twilio_account_id=ta.id, phone_number="+19165550777",
                               friendly_name="Sales", is_active=True, voice_enabled=True)
    db.session.add(second); db.session.flush()
    db.session.add(PhoneNumberUserPermission(company_id=co.id, user_id=u.id, phone_number_id=second.id,
                                             can_access_pwa=True, can_call=True, can_view_calls=True,
                                             can_view_sms=True, can_send_sms=True))
    co2, _ta2, pn2 = _tenant("Other", OTHER_NUMBER)
    db.session.commit()
    calls = []
    monkeypatch.setattr("services.sms_send_idempotency.sendConversationSms", _fake_send(calls), raising=False)
    monkeypatch.setattr("twilio_sms.sendConversationSms", _fake_send(calls))
    tokens = _signed_in(client)
    send = lambda **b: client.post("/api/mobile/v1/messages", headers=_auth(tokens), json={"to": "+14155550111", "body": "hi", **b})
    r = send(idempotency_key="k" * 20, from_phone_number_id=second.id)
    assert r.status_code == 200, r.get_json()
    conv = db.session.get(TwilioConversation, r.get_json()["conversation_id"])
    assert conv.to_number == "+19165550777" and conv.phone_number_id == second.id
    assert calls[-1]["conversation_id"] == conv.id
    # One thread per customer: choosing the primary line moves the thread to it (as an inbound text would).
    r2 = send(idempotency_key="m" * 20, from_phone_number_id=pn.id)
    assert r2.get_json()["conversation_id"] == conv.id
    db.session.refresh(conv)
    assert conv.to_number == NUMBER and conv.phone_number_id == pn.id
    assert send(idempotency_key="n" * 20, from_phone_number_id=pn2.id).status_code == 403  # other tenant's number
    PhoneNumberUserPermission.query.filter_by(user_id=u.id, phone_number_id=second.id).update({"can_send_sms": False})
    db.session.commit()
    assert send(idempotency_key="p" * 20, from_phone_number_id=second.id).status_code == 403


# ══ sign-in hardening (security release parity) ═══════════════════════════ #

@pytest.fixture
def limited_client(monkeypatch):
    from extensions import limiter
    monkeypatch.setenv("LUXIT_RATELIMIT_IN_TESTS", "1")
    a = create_app()
    a.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                    WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    with a.app_context():
        db.create_all()
        limiter.reset()
        yield a.test_client()
        limiter.reset()
        db.session.remove()
        db.drop_all()


def test_mobile_login_has_the_web_login_rate_limit_and_ignores_forwarded_for(limited_client):
    codes = [limited_client.post("/api/mobile/v1/auth/login", json={"email": f"nobody{i}@t.co", "password": "x"},
                                 headers={"X-Real-IP": "203.0.113.20", "X-Forwarded-For": f"198.51.100.{i}"}).status_code
             for i in range(11)]
    assert 429 not in codes[:10]
    r = limited_client.post("/api/mobile/v1/auth/login", json={"email": "z@t.co", "password": "x"},
                            headers={"X-Real-IP": "203.0.113.20", "X-Forwarded-For": "198.51.100.250"})
    assert r.status_code == 429 and r.get_json()["code"] == "too_many_attempts"
    other = limited_client.post("/api/mobile/v1/auth/login", json={"email": "z@t.co", "password": "x"},
                                headers={"X-Real-IP": "203.0.113.21"})
    assert other.status_code == 401


def test_refresh_is_rate_limited(limited_client):
    codes = [limited_client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": f"lxr_{i}"},
                                 headers={"X-Real-IP": "203.0.113.30"}).status_code for i in range(31)]
    assert codes[:30] == [401] * 30 and codes[30] == 429


def test_spoofed_forwarded_for_cannot_dodge_the_per_ip_throttle(app, client, monkeypatch):
    from models import MobileAuthAttempt
    monkeypatch.setattr(mobile_auth, "MAX_FAILURES_PER_IP", 3)
    for i in range(3):
        r = client.post("/api/mobile/v1/auth/login", json={"email": f"guess{i}@t.co", "password": "x"},
                        headers={"X-Real-IP": "203.0.113.40", "X-Forwarded-For": f"10.0.0.{i}"})
        assert r.status_code == 401
    assert {a.ip for a in MobileAuthAttempt.query.all()} == {"203.0.113.40"}
    r = client.post("/api/mobile/v1/auth/login", json={"email": "fresh@t.co", "password": "x"},
                    headers={"X-Real-IP": "203.0.113.40", "X-Forwarded-For": "10.9.9.9"})
    assert r.status_code == 429


def test_throttle_is_checked_before_the_password_hash(app, client, monkeypatch):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    for _ in range(mobile_auth.MAX_FAILURES_PER_IDENTIFIER):
        _login(client, password="wrong")
    called = []
    monkeypatch.setattr(mobile_auth, "verify_credentials", lambda *a: called.append(a))
    assert _login(client).status_code == 429
    assert called == []


def test_successful_sign_in_clears_failures_for_that_account(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    for _ in range(mobile_auth.MAX_FAILURES_PER_IDENTIFIER - 1):
        assert _login(client, password="wrong").status_code == 401
    assert _login(client).status_code == 200
    for _ in range(mobile_auth.MAX_FAILURES_PER_IDENTIFIER - 1):
        assert _login(client, password="wrong").status_code == 401
    assert _login(client).status_code == 200


def test_unknown_account_and_wrong_password_look_the_same(app, client, monkeypatch):
    import werkzeug.security as ws
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    real, checks = ws.check_password_hash, []
    monkeypatch.setattr(ws, "check_password_hash", lambda h, p: checks.append(1) or real(h, p))
    wrong = _login(client, password="wrong")
    unknown = _login(client, name="nosuchuser", password="wrong")
    body = lambda r: {k: v for k, v in r.get_json().items() if k != "request_id"}  # request_id is per request
    assert (wrong.status_code, body(wrong)) == (unknown.status_code, body(unknown))
    assert len(checks) == 2  # the unknown account still spends a hash


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "Bearer lxa_not-a-real-token", "Bearer garbage",
                                    "Basic dXNlcjpwYXNz", "bearer {access}", "Bearer {refresh}", "Token {access}"])
def test_malformed_or_wrong_type_tokens_are_rejected(app, client, header):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    value = header.format(access=tokens["access_token"], refresh=tokens["refresh_token"])
    r = client.get("/api/mobile/v1/me", headers={"Authorization": value} if value else {})
    assert r.status_code == 401 and r.get_json()["success"] is False


def test_revoked_session_access_token_is_rejected(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    MobileSession.query.update({"revoked_at": datetime.utcnow(), "revoke_reason": "test"})
    db.session.commit()
    assert client.get("/api/mobile/v1/me", headers=_auth(tokens)).status_code == 401


def test_cookie_of_another_user_is_ignored_when_a_bearer_token_is_sent(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    co_a, _ta, pn_a = _tenant("A"); _user(co_a, pn_a, "alice")
    co_b, _tb, pn_b = _tenant("B", OTHER_NUMBER); bob = _user(co_b, pn_b, "bob")
    db.session.commit()
    alice = _signed_in(client, "alice")
    with client.session_transaction() as s:
        s["_user_id"] = str(bob.id); s["_fresh"] = True
    me = client.get("/api/mobile/v1/me", headers=_auth(alice)).get_json()
    assert me["company"]["id"] == co_a.id and me["user"]["email"] == "alice@t.co"
    # A cookie-only write on the CSRF-exempt prefix is refused before any view runs.
    assert client.patch("/api/mobile/v1/pwa/preferences", json={"x": 1}).status_code == 401


def test_company_call_routing_is_not_exposed_on_the_mobile_prefix(app, client):
    co, _ta, pn = _tenant(); _admin(co, pn); db.session.commit()
    boss = _signed_in(client, "boss")
    for method in ("get", "put"):
        r = getattr(client, method)("/api/mobile/v1/phone/settings", headers=_auth(boss), json={"forward_number": "+14155550100"})
        assert r.status_code in (404, 405)



# ══ push-token storm protection ═══════════════════════════════════════════ #

def _put_token(client, tokens, token="a" * 64, **extra):
    return client.put("/api/mobile/v1/devices/current/push-token", headers=_auth(tokens),
                      json={"token": token, "type": "apns", **extra})


def test_identical_push_token_is_acknowledged_without_a_write(app, client):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    assert _put_token(client, tokens, apns_environment="production").status_code == 200
    first = MobileDevice.query.one().push_token_updated_at
    for _ in range(5):
        r = _put_token(client, tokens, apns_environment="production")
        assert r.status_code == 200 and r.get_json()["success"] is True
    db.session.expire_all()
    assert MobileDevice.query.one().push_token_updated_at == first


@pytest.mark.parametrize("change", ["token", "environment", "cleared", "stale"])
def test_changed_or_stale_push_token_is_written(app, client, change):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    assert _put_token(client, tokens, apns_environment="sandbox").status_code == 200
    if change == "stale":
        MobileDevice.query.update({"push_token_updated_at": datetime.utcnow() - timedelta(minutes=11)}); db.session.commit()
    before = MobileDevice.query.one().push_token_updated_at
    if change == "token":
        r = _put_token(client, tokens, token="b" * 64, apns_environment="sandbox")
    elif change == "cleared":
        r = client.put("/api/mobile/v1/devices/current/push-token", headers=_auth(tokens), json={"token": None})
    else:
        r = _put_token(client, tokens, apns_environment="production" if change == "environment" else "sandbox")
    assert r.status_code == 200
    db.session.expire_all()
    device = MobileDevice.query.one()
    assert device.push_token_updated_at > before
    assert device.push_token == {"token": "b" * 64, "cleared": None}.get(change, "a" * 64)
    assert device.apns_environment == ("production" if change == "environment" else "sandbox")


def test_push_token_is_rate_limited_per_handset_not_per_ip(limited_client):
    client = limited_client
    co, _ta, pn = _tenant(); _user(co, pn, "anna"); _user(co, pn, "ben"); db.session.commit()
    ip = {"X-Real-IP": "203.0.113.50"}
    a = client.post("/api/mobile/v1/auth/login", headers=ip, json={
        "email": "anna@t.co", "password": "pw-123456",
        "device": {"device_uid": UID_A, "platform": "ios", "device_name": "A", "app_version": "1.0.0"}}).get_json()
    b = client.post("/api/mobile/v1/auth/login", headers=ip, json={
        "email": "ben@t.co", "password": "pw-123456",
        "device": {"device_uid": "ios-install-cccccccccccccccc", "platform": "ios", "device_name": "B",
                   "app_version": "1.0.0"}}).get_json()
    put = lambda t, tok: client.put("/api/mobile/v1/devices/current/push-token",
                                    headers={**_auth(t), **ip}, json={"token": tok, "type": "apns"})
    codes = [put(a, "a" * 64).status_code for _ in range(12)]
    assert codes == [200] * 12
    r = put(a, "a" * 64)
    assert r.status_code == 429 and r.get_json()["code"] == "too_many_attempts"
    assert r.headers["Retry-After"] == "60"
    assert put(b, "b" * 64).status_code == 200  # same carrier IP, other handset


@pytest.mark.parametrize("ended", ["logout", "revoked", "expired"])
def test_ended_session_cannot_register_a_push_token(app, client, ended):
    co, _ta, pn = _tenant(); _user(co, pn); db.session.commit()
    tokens = _signed_in(client)
    if ended == "logout":
        assert client.post("/api/mobile/v1/auth/logout", headers=_auth(tokens)).status_code == 200
    else:
        field = "revoked_at" if ended == "revoked" else "access_expires_at"
        MobileSession.query.update({field: datetime.utcnow() - timedelta(seconds=1)}); db.session.commit()
    r = _put_token(client, tokens)
    assert r.status_code == 401
    db.session.expire_all()
    assert MobileDevice.query.one().push_token is None


@pytest.mark.parametrize("kind", ["push", "voice"])
@pytest.mark.parametrize("holder", ["live", "same_install", "signed_out"])
def test_cross_tenant_handset_claim(app, client, kind, holder):
    """Another tenant's live install keeps its token; same install or a dead row is retired."""
    co1, _ta, pn1 = _tenant("A"); _user(co1, pn1, "anna")
    co2, _ta2, pn2 = _tenant("B", number="+14155550199"); _user(co2, pn2, "zed"); db.session.commit()
    anna = _signed_in(client, "anna", UID_A)
    claim = lambda tokens: (_put_token(client, tokens, token="v" * 64) if kind == "push" else
                            client.post("/api/mobile/v1/devices/current/voice-registration", headers=_auth(tokens),
                                        json={"registered": True, "voice_device_token": "v" * 64}))
    assert claim(anna).status_code == 200
    if holder == "signed_out":
        assert client.post("/api/mobile/v1/auth/logout", headers=_auth(anna)).status_code == 200
        if kind == "voice":  # logout clears the push token itself; keep a stale Voice hash
            MobileDevice.query.filter_by(company_id=co1.id).update({"voice_token_hash": hashlib.sha256(b"v" * 64).hexdigest()})
            db.session.commit()
    zed = _signed_in(client, "zed", UID_A if holder == "same_install" else UID_B)
    r = claim(zed)
    db.session.expire_all()
    anna_device = MobileDevice.query.filter_by(company_id=co1.id).one()
    zed_device = MobileDevice.query.filter_by(company_id=co2.id).one()
    if holder == "live":
        assert r.status_code == 409 and r.get_json()["code"] == "conflict"
        assert anna_device.status == "active"
        assert (anna_device.push_token if kind == "push" else anna_device.voice_token_hash) is not None
        assert zed_device.push_token is None and zed_device.voice_registered_at is None
        assert client.get("/api/mobile/v1/me", headers=_auth(anna)).status_code == 200
    else:
        assert r.status_code == 200
        assert anna_device.push_token is None and anna_device.voice_token_hash is None
        assert anna_device.status == "signed_out"


def test_non_platform_workspace_gets_no_native_voice_token(app, client, monkeypatch):
    """T-16 on the native path: the Voice token is minted on the platform
    account, so a tenant without a platform sender is refused with a 403 and
    /me reports calling unavailable instead of crashing."""
    monkeypatch.delenv("LUXIT_PLATFORM_SENDER_COMPANY_IDS", raising=False)
    co_a, _ta, pn_a = _tenant("A"); _user(co_a, pn_a, "alice")
    co_b, _tb, pn_b = _tenant("B", OTHER_NUMBER); _user(co_b, pn_b, "bob")
    db.session.commit()
    bob = _signed_in(client, "bob", UID_B, "android")
    r = client.get("/api/mobile/v1/voice/token", headers=_auth(bob))
    assert r.status_code == 403
    assert r.get_json()["code"] == "CALLING_NOT_PROVISIONED"
    assert "token" not in r.get_json()
    me = client.get("/api/mobile/v1/me", headers=_auth(bob))
    assert me.status_code == 200
    calling = me.get_json()["calling"]
    assert calling["available"] is False and calling["reason"] == "CALLING_NOT_PROVISIONED"
    alice = _signed_in(client, "alice")
    assert client.get("/api/mobile/v1/voice/token", headers=_auth(alice)).status_code == 200


def test_native_login_requires_verified_email_and_is_audited(app, client):
    """Native sign-in keeps the web login's email-verification gate and
    writes the same security audit trail (T-26)."""
    from models import SecurityAuditEvent, UserEmailVerification
    co, _ta, pn = _tenant("A")
    u = _user(co, pn, "alice")
    db.session.add(UserEmailVerification(user_id=u.id, email=u.email))
    db.session.commit()
    r = _login(client, "alice")
    assert r.status_code == 403 and r.get_json()["code"] == "email_unverified"
    assert MobileSession.query.count() == 0
    assert _login(client, "alice", password="wrong-password").status_code == 401
    db.session.get(UserEmailVerification, u.id).verified_at = datetime.utcnow()
    db.session.commit()
    assert _login(client, "alice").status_code == 200
    events = SecurityAuditEvent.query.filter_by(action="auth.login").order_by(SecurityAuditEvent.id).all()
    assert [e.outcome for e in events] == ["denied", "failure", "success"]
    assert all('"via":"mobile"' in (e.detail or "") for e in events)
    assert events[0].detail and "email_unverified" in events[0].detail
    assert events[2].actor_user_id == u.id and events[2].company_id == co.id


def test_password_change_signs_out_phones_but_a_new_sign_in_rings(app, client, monkeypatch):
    """Changing the password in the web app ends LUX Connect sessions (API
    and ringing) and is audited; signing in again restores ringing."""
    from flask_login import FlaskLoginClient
    from models import SecurityAuditEvent
    co, _ta, pn = _tenant(); u = _user(co, pn); db.session.commit()
    old = _signed_in(client)
    _register_voice(client, old)
    device = MobileDevice.query.one()
    identity = mobile_voice_identity(co.id, u.id, device.id, UID_A)
    assert identity in _identities(_inbound(client, monkeypatch, sid="CAbefore"))
    app.test_client_class = FlaskLoginClient
    with app.test_client(user=u) as web:
        r = web.post("/user/change-password", data={"current_password": "pw-123456",
                                                    "new_password": "new-pw-654321",
                                                    "confirm_password": "new-pw-654321"})
        assert r.status_code == 302
        assert web.get("/user/change-password").status_code == 200  # this browser stays signed in
    from flask import g
    for key in list(vars(g)):  # one app context for the whole test: don't leak the web request's g
        g.pop(key, None)
    assert client.get("/api/mobile/v1/me", headers=_auth(old)).status_code == 401
    assert client.post("/api/mobile/v1/auth/refresh", json={"refresh_token": old["refresh_token"]}).status_code == 401
    after = _inbound(client, monkeypatch, sid="CAafter")
    assert "<Dial" in after and identity not in _identities(after)
    assert SecurityAuditEvent.query.filter_by(action="auth.password_changed", target_user_id=u.id).count() == 1
    r = _login(client, password="new-pw-654321")
    assert r.status_code == 200
    _register_voice(client, r.get_json(), voice_token="voip-token-2")
    for key in list(vars(g)):
        g.pop(key, None)
    assert identity in _identities(_inbound(client, monkeypatch, sid="CAagain"))
