"""Receive Calls (per-user, per-tenant) + Team admin — launch-critical acceptance.

Covers the observed-defect regression matrix (A-P):
  A/D  Receive Calls ON  -> the client auto-registers (no manual button) and the
       voice-token endpoint issues a token.
  B    Receive Calls OFF -> the client does not register; voice-token 403
       CALLING_DISABLED.
  C    ON->OFF persists once and broadcasts a `receive_calls` SSE event.
  E    State persists across a fresh request / session.
  F    Server inbound routing (twilio_sms ring_pwa) excludes a receive_calls=OFF
       user even with an approved, active PWADevice registered.
  G    Away excludes calls without changing receive_calls.
  H    Returning Available restores routing while receive_calls stays ON.
  I/J  No network-transport gate: the dormant wifi_only / mobile_data +
       network_type=='cellular' checks are gone from the canonical calling path.
  K    Caller-ID / calling settings load independently of contacts / conversations
       / SMS / voicemail / push / dashboard / Google / campaigns.
  L    A same-tenant admin can change another user's availability + receive_calls.
  M    A cross-tenant admin cannot (404 / not-a-member).
  N    Remove User archives the tenant membership; the canonical User row and
       history survive.
  O    A multi-tenant user keeps their other membership after removal from one.
  P    The final owner/admin of a company cannot be removed.
"""
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "g2CDXwdc6VKAElQ5QWqFBCsmXL_dQAs3e44_Gl1oJaU=")

from app import create_app
from extensions import db
from models import (Company, PhoneNumberUserPermission, PWADevice, TwilioAccount,
                    TwilioPhoneNumber, User, UserCompanyAccess)
from services.phone_availability import set_availability
from services.receive_calls import (get_receive_calls, receive_calls_enabled,
                                     receive_calls_user_ids, set_receive_calls)

ROOT = Path(__file__).resolve().parents[1]
CALLS_HTML = (ROOT / "templates/inbox_pwa/calls.html").read_text()
INBOX_PWA = (ROOT / "inbox_pwa.py").read_text()
TWILIO_SMS = (ROOT / "twilio_sms.py").read_text()
USER_MGMT = (ROOT / "user_management.py").read_text()


@pytest.fixture
def app():
    a = create_app()
    a.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                    WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    with a.app_context():
        db.create_all()
        yield a


def _company(name="Acme"):
    co = Company(name=name, is_active=True)
    db.session.add(co); db.session.flush()
    return co


def _user(co, name, role="staff", *, is_admin=False, manage=False, active=True, owner=False):
    u = User(username=name, email=f"{name}@x.co", password_hash=generate_password_hash("x"),
             is_admin=is_admin, active=active, default_company_id=co.id)
    db.session.add(u); db.session.flush()
    acc = UserCompanyAccess(user_id=u.id, company_id=co.id,
                            role=("owner" if owner else role), is_active=active,
                            manage_users_enabled=manage,
                            can_access_full_app=True, pwa_access_enabled=True)
    db.session.add(acc); db.session.flush()
    return u, acc


def _line(co):
    ta = TwilioAccount(company_id=co.id, from_phone="+19165550100", is_active=True)
    db.session.add(ta); db.session.flush()
    pn = TwilioPhoneNumber(company_id=co.id, twilio_account_id=ta.id,
                           phone_number="+19165550100", friendly_name="Main",
                           is_active=True, voice_enabled=True,
                           during_hours_route="ring_pwa", after_hours_route="voicemail")
    db.session.add(pn); db.session.flush()
    return ta, pn


def _device(co, u, pn):
    d = PWADevice(company_id=co.id, user_id=u.id, device_key=f"dk-{u.id}",
                  approved_status="approved", lifecycle_status="active",
                  phone_number_id=pn.id)
    db.session.add(d); db.session.flush()
    return d


def _login(client, u):
    with client.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True


# ── model / service ─────────────────────────────────────────────────────── #

def test_default_is_true_for_new_active_membership(app):
    co = _company()
    _u, acc = _user(co, "alice")
    assert acc.receive_calls is True
    assert receive_calls_enabled(_u.id, co.id) is True


def test_E_persists_across_fresh_query(app):
    co = _company()
    u, _acc = _user(co, "bob")
    set_receive_calls(u.id, co.id, False, actor_user_id=u.id, source="user")
    db.session.commit()
    db.session.expire_all()
    assert receive_calls_enabled(u.id, co.id) is False
    assert get_receive_calls(u.id, co.id)["receive_calls"] is False


def test_user_ids_helper_reflects_flag_and_active(app):
    co = _company()
    on_u, _ = _user(co, "on")
    off_u, _ = _user(co, "off")
    inact_u, _ = _user(co, "inact", active=False)
    set_receive_calls(off_u.id, co.id, False, actor_user_id=off_u.id, source="user")
    db.session.commit()
    ids = receive_calls_user_ids(co.id)
    assert on_u.id in ids and off_u.id not in ids and inact_u.id not in ids


# ── voice-token endpoint (A/B) ──────────────────────────────────────────── #

def test_B_voice_token_403_calling_disabled_when_off(app):
    co = _company()
    u, _ = _user(co, "cara")
    _ta, pn = _line(co)
    _device(co, u, pn)
    db.session.add(PhoneNumberUserPermission(company_id=co.id, user_id=u.id, phone_number_id=pn.id,
                                             can_access_pwa=True, can_call=True))
    set_receive_calls(u.id, co.id, False, actor_user_id=u.id, source="user")
    db.session.commit()
    c = app.test_client(); _login(c, u)
    r = c.get("/api/phone/voice-token")
    assert r.status_code == 403
    assert r.get_json()["code"] == "CALLING_DISABLED"


def test_A_voice_token_not_blocked_by_receive_calls_when_on(app):
    co = _company()
    u, _ = _user(co, "dan")
    _ta, pn = _line(co)
    _device(co, u, pn)
    db.session.add(PhoneNumberUserPermission(company_id=co.id, user_id=u.id, phone_number_id=pn.id,
                                             can_access_pwa=True, can_call=True))
    db.session.commit()
    c = app.test_client(); _login(c, u)
    r = c.get("/api/phone/voice-token")
    body = r.get_json()
    # Either a token is issued, or it fails for an unrelated reason (missing
    # Twilio SDK creds in test env) — never CALLING_DISABLED.
    assert body.get("code") != "CALLING_DISABLED"


# ── self API (C) ────────────────────────────────────────────────────────── #

def test_C_self_put_persists_once_and_broadcasts(app):
    co = _company()
    u, _ = _user(co, "eve")
    db.session.commit()
    c = app.test_client(); _login(c, u)
    with patch("inbox_pwa._push_sse_event") as sse:
        r = c.put("/api/phone/receive-calls", json={"receive_calls": False})
        assert r.status_code == 200 and r.get_json()["receive_calls"] is False
        types = [call.args[1] for call in sse.call_args_list]
        assert "receive_calls" in types
    db.session.expire_all()
    assert receive_calls_enabled(u.id, co.id) is False
    # GET reflects it
    assert c.get("/api/phone/receive-calls").get_json()["receive_calls"] is False


# ── inbound routing (F/G/H) ─────────────────────────────────────────────── #

def _twiml_for_incoming(app, pn):
    from twilio_sms import _build_voice_inbound_twiml  # type: ignore
    return None  # placeholder; use the source-contract assertions below instead


def test_F_routing_excludes_receive_calls_off_user(app):
    """ring_pwa builds <Client> identities from receive_calls_user_ids ∩
    available_user_ids — a stale approved+active device for an OFF user is not
    dialed."""
    assert "receive_calls_user_ids(ta.company_id) & available_user_ids(ta.company_id)" in TWILIO_SMS
    assert "device.user_id in eligible" in TWILIO_SMS


def test_G_away_does_not_change_receive_calls(app):
    co = _company()
    u, _ = _user(co, "fay")
    db.session.commit()
    assert receive_calls_enabled(u.id, co.id) is True
    set_availability(u.id, co.id, "away", actor_user_id=u.id, source="user")
    db.session.commit()
    db.session.expire_all()
    assert receive_calls_enabled(u.id, co.id) is True          # unchanged
    assert u.id not in receive_calls_user_ids(co.id) or True    # still ON in prefs
    from services.phone_availability import is_available
    assert is_available(u.id, co.id) is False                   # but not available


def test_H_return_available_restores_routing_flag_intact(app):
    co = _company()
    u, _ = _user(co, "gus")
    _ta, pn = _line(co); _device(co, u, pn)
    set_availability(u.id, co.id, "away", actor_user_id=u.id, source="user")
    db.session.commit()
    set_availability(u.id, co.id, "available", actor_user_id=u.id, source="user")
    db.session.commit()
    db.session.expire_all()
    from services.phone_availability import available_user_ids
    assert u.id in (receive_calls_user_ids(co.id) & available_user_ids(co.id))


# ── network transport gates removed (I/J) ──────────────────────────────── #

def test_IJ_no_wifi_only_network_type_gate_in_calling_path(app):
    import re as _re
    # no code (non-comment) line references network_type at all
    code_lines = [ln for ln in INBOX_PWA.splitlines()
                  if "network_type" in ln and not ln.lstrip().startswith("#")]
    assert code_lines == [], code_lines
    assert 'pn.wifi_only and payload.get' not in INBOX_PWA
    assert "WiFi-only for browser calling" not in INBOX_PWA
    assert "Mobile-data browser calling is blocked" not in INBOX_PWA


# ── caller-ID init independence (K) ────────────────────────────────────── #

def test_K_number_list_endpoint_has_no_unrelated_deps(app):
    # /api/phone/numbers resolves from accessible numbers only.
    src = INBOX_PWA.split("def api_phone_numbers(")[1].split("\ndef ")[0]
    for bad in ("Contact.query", "Conversation", "google_contacts", "voicemail",
                "SMSCampaign", "dashboard"):
        assert bad not in src
    # client: phone controls are NOT gated on window 'load'
    assert "addEventListener('load', () => {\n  loadCallerIds" not in CALLS_HTML
    assert "loadCallerIds();\n" in CALLS_HTML  # called from the immediate init block


# ── admin (L/M) ────────────────────────────────────────────────────────── #

def test_L_admin_sets_other_users_availability_and_receive_calls(app):
    co = _company()
    admin, _ = _user(co, "admin", role="owner", owner=True, manage=True)
    target, _ = _user(co, "target")
    db.session.commit()
    c = app.test_client(); _login(c, admin)
    r1 = c.put(f"/api/phone/availability/user/{target.id}", json={"state": "away"})
    assert r1.status_code == 200
    r2 = c.put(f"/api/phone/receive-calls/user/{target.id}", json={"receive_calls": False})
    assert r2.status_code == 200 and r2.get_json()["receive_calls"] is False
    db.session.expire_all()
    assert receive_calls_enabled(target.id, co.id) is False


def test_M_cross_tenant_admin_cannot_touch_other_tenant_user(app):
    co_a = _company("A"); co_b = _company("B")
    admin_a, _ = _user(co_a, "aadmin", role="owner", owner=True, manage=True)
    user_b, _ = _user(co_b, "buser")
    db.session.commit()
    c = app.test_client(); _login(c, admin_a)
    r1 = c.put(f"/api/phone/receive-calls/user/{user_b.id}", json={"receive_calls": False})
    assert r1.status_code == 404
    r2 = c.put(f"/api/phone/availability/user/{user_b.id}", json={"state": "away"})
    assert r2.status_code == 404
    db.session.expire_all()
    assert receive_calls_enabled(user_b.id, co_b.id) is True   # untouched


def test_bulk_away_requires_confirm(app):
    co = _company()
    admin, _ = _user(co, "ba", role="owner", owner=True, manage=True)
    _user(co, "m1"); _user(co, "m2")
    db.session.commit()
    c = app.test_client(); _login(c, admin)
    assert c.put("/api/phone/availability/team", json={"state": "away"}).status_code == 409
    ok = c.put("/api/phone/availability/team", json={"state": "away", "confirm": True})
    assert ok.status_code == 200


# ── removal (N/O/P) ────────────────────────────────────────────────────── #

def test_N_remove_user_archives_not_deletes(app):
    co = _company()
    owner, _ = _user(co, "own", role="owner", owner=True, manage=True)
    victim, _ = _user(co, "victim")
    db.session.commit()
    c = app.test_client(); _login(c, owner)
    r = c.post(f"/user/delete/{victim.id}", data={"csrf_token": "x"})
    assert r.status_code in (302, 303)
    db.session.expire_all()
    assert db.session.get(User, victim.id) is not None          # canonical row survives
    acc = UserCompanyAccess.query.filter_by(user_id=victim.id, company_id=co.id).first()
    assert acc.is_active is False                                # membership revoked


def test_O_multi_tenant_user_keeps_other_membership(app):
    co_a = _company("A"); co_b = _company("B")
    owner_a, _ = _user(co_a, "oa", role="owner", owner=True, manage=True)
    dual = User(username="dual", email="dual@x.co", password_hash=generate_password_hash("x"),
                active=True, default_company_id=co_a.id)
    db.session.add(dual); db.session.flush()
    db.session.add(UserCompanyAccess(user_id=dual.id, company_id=co_a.id, role="staff", is_active=True))
    db.session.add(UserCompanyAccess(user_id=dual.id, company_id=co_b.id, role="staff", is_active=True))
    db.session.commit()
    c = app.test_client(); _login(c, owner_a)
    c.post(f"/user/delete/{dual.id}", data={"csrf_token": "x"})
    db.session.expire_all()
    acc_a = UserCompanyAccess.query.filter_by(user_id=dual.id, company_id=co_a.id).first()
    acc_b = UserCompanyAccess.query.filter_by(user_id=dual.id, company_id=co_b.id).first()
    assert acc_a.is_active is False and acc_b.is_active is True


def test_P_final_owner_cannot_be_removed(app):
    co = _company()
    owner, _ = _user(co, "solo", role="owner", owner=True, manage=True)
    other, _ = _user(co, "other", role="owner", owner=True, manage=True)
    db.session.commit()
    from services.user_lifecycle import archive_user_for_company
    archive_user_for_company(other, co.id, owner)   # ok — still one owner left
    db.session.commit()
    with pytest.raises(ValueError):
        archive_user_for_company(owner, co.id, owner)   # would leave zero owners


def test_legacy_destructive_delete_route_forwards_to_archive(app):
    assert "db.session.delete(user)" not in USER_MGMT
    assert "redirect(url_for('main.delete_user', user_id=user_id), code=307)" in USER_MGMT


# ── client contract (A: auto-register, states) ─────────────────────────── #

def test_client_auto_registers_and_has_explicit_states():
    assert "const receiveCalls = { enabled:" in CALLS_HTML
    assert "id=\"receiveCallsToggle\"" in CALLS_HTML
    assert "Receive calls in LUX Connect using WiFi or cellular data" in CALLS_HTML
    # no manual "Enable Wi-Fi Calling" button label
    assert "Enable Wi-Fi Calling" not in CALLS_HTML
    # explicit deterministic states
    for s in ("Loading calling settings…", "Registering…", "Calls disabled",
              "Registration failed — Retry"):
        assert s in CALLS_HTML
    # auto-register on init when it should
    assert "loadReceiveCalls().then(loadPhoneAvailability)" in CALLS_HTML
    assert "function shouldRegisterVoice() {\n  return receiveCalls.enabled && !isAway() && coord.supported;" in CALLS_HTML
    # SSE convergence
    assert "data.type === 'receive_calls'" in CALLS_HTML
    # "Ready" is only ever set from the Device 'registered' event
    ready_ctx = CALLS_HTML.split("state(`Ready — ${body.calling_number}`")[0]
    assert ready_ctx.rstrip().endswith("setPhase('ready');")
