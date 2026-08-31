"""Shared Phone User Availability — Available / Away.

AWAY = communications availability (NOT account disable). It pauses ringing /
incoming-call UI / phone push+badges for a user on a tenant's shared lines and
must fail BEFORE any voice-token mint or Twilio.Device registration. It never
touches is_active / role / membership / SMS consent / the business number, and
never affects other users on the line.
"""
import os
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "g2CDXwdc6VKAElQ5QWqFBCsmXL_dQAs3e44_Gl1oJaU=")

from app import create_app
from extensions import db
from models import (Company, PWADevice, TwilioAccount, TwilioPhoneNumber, User,
                    UserCompanyAccess)
from services.phone_availability import (AVAILABLE, AWAY, available_user_ids,
                                         get_availability, is_available,
                                         set_availability, team_availability,
                                         can_admin_manage, normalize_state,
                                         AvailabilityError)

CALLS_HTML = (Path(__file__).resolve().parents[1] / "templates/inbox_pwa/calls.html").read_text()
TWILIO_SMS = (Path(__file__).resolve().parents[1] / "twilio_sms.py").read_text()
INBOX_PWA = (Path(__file__).resolve().parents[1] / "inbox_pwa.py").read_text()


@pytest.fixture
def app_ctx():
    app = create_app()
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                      WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        co = Company(name="Shared Line Co", is_active=True)
        other = Company(name="Other Co", is_active=True)
        db.session.add_all([co, other]); db.session.flush()
        acct = TwilioAccount(company_id=co.id, from_phone="+19165989519", is_active=True)
        db.session.add(acct); db.session.flush()
        line = TwilioPhoneNumber(company_id=co.id, twilio_account_id=acct.id,
                                 phone_number="+19165989519", friendly_name="Shared",
                                 voice_enabled=True, is_active=True, during_hours_route="ring_pwa",
                                 after_hours_route="voicemail",
                                 voicemail_greeting_text="Please leave a message.")
        db.session.add(line); db.session.flush()

        def mk(name, cid, role="staff", admin=False, manage=False):
            u = User(username=name, email=f"{name}@x.com",
                     password_hash=generate_password_hash("pw"), is_admin=admin, default_company_id=cid)
            db.session.add(u); db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=cid, role=role, is_default=True,
                                             pwa_access_enabled=True, can_access_full_app=True,
                                             manage_users_enabled=manage))
            db.session.add(PWADevice(company_id=cid, user_id=u.id, device_key=f"dev-{name}",
                                     approved_status="approved", lifecycle_status="active"))
            return u

        A = mk("alice", co.id, role="owner")
        B = mk("bob", co.id)
        C = mk("carol", co.id)
        admin = mk("dave", co.id, role="admin", manage=True)
        outsider = mk("erin", other.id, role="admin", admin=True)
        db.session.commit()
        yield dict(app=app, co=co.id, other=other.id, line=line.id,
                   A=A.id, B=B.id, C=C.id, admin=admin.id, outsider=outsider.id)
        db.session.remove(); db.drop_all()


def _login(client, uid):
    with client.session_transaction() as s:
        s["_user_id"] = str(uid); s["_fresh"] = True; s["user_id"] = uid; s["logged_in"] = True


# ── service / state ──────────────────────────────────────────────────────── #

def test_default_is_available_and_states_validate():
    assert normalize_state("Available") == AVAILABLE
    assert normalize_state("AWAY") == AWAY
    with pytest.raises(AvailabilityError):
        normalize_state("disabled")


def test_user_sets_self_away_then_available(app_ctx):
    co, B = app_ctx["co"], app_ctx["B"]
    assert is_available(B, co) is True
    r = set_availability(B, co, "away", actor_user_id=B, source="user")
    assert r["state"] == "away" and r["available"] is False and r["source"] == "user"
    assert is_available(B, co) is False
    set_availability(B, co, "available", actor_user_id=B, source="user")
    assert is_available(B, co) is True


def test_away_does_not_touch_account_or_consent(app_ctx):
    co, B = app_ctx["co"], app_ctx["B"]
    acc = UserCompanyAccess.query.filter_by(user_id=B, company_id=co).first()
    user = db.session.get(User, B)
    before = (acc.is_active, acc.role, user.active, user.default_company_id)
    set_availability(B, co, "away", actor_user_id=B, source="user"); db.session.commit()
    acc = UserCompanyAccess.query.filter_by(user_id=B, company_id=co).first()
    user = db.session.get(User, B)
    assert (acc.is_active, acc.role, user.active, user.default_company_id) == before


def test_available_user_ids_excludes_away_only(app_ctx):
    co, A, B, C = app_ctx["co"], app_ctx["A"], app_ctx["B"], app_ctx["C"]
    set_availability(B, co, "away", actor_user_id=B, source="user"); db.session.commit()
    ids = available_user_ids(co)
    assert A in ids and C in ids and B not in ids


def test_set_availability_rejects_non_member(app_ctx):
    with pytest.raises(AvailabilityError):
        set_availability(app_ctx["outsider"], app_ctx["co"], "away",
                         actor_user_id=app_ctx["outsider"], source="user")


# ── shared-line routing ──────────────────────────────────────────────────── #

def _post_inbound(client, to, sid):
    return client.post("/twilio/voice/inbound", data={"To": to, "From": "+14155551212",
                                                      "CallSid": sid, "Direction": "inbound"})


def test_available_user_rings_away_user_does_not(app_ctx):
    app, co = app_ctx["app"], app_ctx["co"]
    import twilio_sms
    from services.phone_identity import pwa_voice_identity
    client = app.test_client()
    with app.app_context():
        twilio_sms._is_business_hours = lambda *a, **k: True
    set_availability(app_ctx["B"], co, "away", actor_user_id=app_ctx["B"], source="user"); db.session.commit()

    body = _post_inbound(client, "+19165989519", "TEST_ROUTE_1").get_data(as_text=True)
    id_a = pwa_voice_identity(co, app_ctx["A"], "dev-alice")
    id_b = pwa_voice_identity(co, app_ctx["B"], "dev-bob")
    id_c = pwa_voice_identity(co, app_ctx["C"], "dev-carol")
    assert id_a in body and id_c in body
    assert id_b not in body, "an AWAY user must not be rung"


def test_all_users_away_falls_through_to_voicemail_not_a_drop(app_ctx):
    app, co = app_ctx["app"], app_ctx["co"]
    import twilio_sms
    client = app.test_client()
    with app.app_context():
        twilio_sms._is_business_hours = lambda *a, **k: True
    for uid in (app_ctx["A"], app_ctx["B"], app_ctx["C"], app_ctx["admin"]):
        set_availability(uid, co, "away", actor_user_id=uid, source="user")
    db.session.commit()

    resp = _post_inbound(client, "+19165989519", "TEST_ALL_AWAY")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "<Record" in body                      # business voicemail flow
    assert "<Client>" not in body                 # nobody is rung
    assert "InFailedSqlTransaction" not in body


# ── voice-token gate ─────────────────────────────────────────────────────── #

def test_voice_token_denied_while_away(app_ctx):
    app, co, B = app_ctx["app"], app_ctx["co"], app_ctx["B"]
    from models import PhoneNumberUserPermission
    db.session.add(PhoneNumberUserPermission(company_id=co, user_id=B, phone_number_id=app_ctx["line"],
                                             can_access_pwa=True, can_view_calls=True, can_call=True))
    db.session.commit()
    client = app.test_client(); _login(client, B)

    # Available: the availability gate does not block (any failure here is an
    # unrelated env issue e.g. missing Twilio SDK creds, never PHONE_AWAY).
    ok = client.get(f"/api/phone/voice-token?device_key=dev-bob")
    assert (ok.get_json() or {}).get("code") != "PHONE_AWAY"

    # Away: blocked before any token mint, with the PHONE_AWAY code.
    set_availability(B, co, "away", actor_user_id=B, source="user"); db.session.commit()
    away = client.get(f"/api/phone/voice-token?device_key=dev-bob")
    assert away.status_code == 403
    assert away.get_json().get("code") == "PHONE_AWAY"


# ── endpoints: self + admin + authz ──────────────────────────────────────── #

def test_self_endpoint_get_and_put(app_ctx):
    client = app_ctx["app"].test_client(); _login(client, app_ctx["B"])
    assert client.get("/api/phone/availability").get_json()["state"] == "available"
    r = client.put("/api/phone/availability", json={"state": "away"})
    assert r.status_code == 200 and r.get_json()["state"] == "away"
    assert client.get("/api/phone/availability").get_json()["state"] == "away"


def test_admin_can_view_and_set_team_member(app_ctx):
    client = app_ctx["app"].test_client(); _login(client, app_ctx["admin"])
    team = client.get("/api/phone/availability/team")
    assert team.status_code == 200 and len(team.get_json()["team"]) >= 4

    r = client.put(f"/api/phone/availability/user/{app_ctx['B']}", json={"state": "away"})
    assert r.status_code == 200 and r.get_json()["state"] == "away" and r.get_json()["source"] == "admin"
    assert is_available(app_ctx["B"], app_ctx["co"]) is False


def test_ordinary_user_cannot_change_another_user(app_ctx):
    client = app_ctx["app"].test_client(); _login(client, app_ctx["B"])
    assert client.get("/api/phone/availability/team").status_code == 403
    r = client.put(f"/api/phone/availability/user/{app_ctx['C']}", json={"state": "away"})
    assert r.status_code == 403
    assert is_available(app_ctx["C"], app_ctx["co"]) is True


def test_cross_tenant_admin_cannot_change_user(app_ctx):
    client = app_ctx["app"].test_client(); _login(client, app_ctx["outsider"])
    # outsider's default company is `other`; the target lives in `co`
    r = client.put(f"/api/phone/availability/user/{app_ctx['B']}", json={"state": "away"})
    assert r.status_code in (403, 404)
    assert is_available(app_ctx["B"], app_ctx["co"]) is True


def test_admin_state_is_recorded_with_provenance(app_ctx):
    r = set_availability(app_ctx["B"], app_ctx["co"], "away",
                         actor_user_id=app_ctx["admin"], source="admin")
    assert r["source"] == "admin" and r["changed_by_user_id"] == app_ctx["admin"]
    assert r["changed_at"] is not None


# ── multi-device (user-scoped) ───────────────────────────────────────────── #

def test_availability_is_user_scoped_not_device_scoped(app_ctx):
    """Two devices for the same user -> one Away flips both out of routing."""
    co, B = app_ctx["co"], app_ctx["B"]
    db.session.add(PWADevice(company_id=co, user_id=B, device_key="dev-bob-2",
                             approved_status="approved", lifecycle_status="active"))
    db.session.commit()
    set_availability(B, co, "away", actor_user_id=B, source="user"); db.session.commit()
    assert B not in available_user_ids(co)


# ── SMS while Away ───────────────────────────────────────────────────────── #

def test_comms_notification_suppressed_for_away_but_record_persists(app_ctx):
    app, co = app_ctx["app"], app_ctx["co"]
    from unittest.mock import patch
    import inbox_pwa
    set_availability(app_ctx["B"], co, "away", actor_user_id=app_ctx["B"], source="user"); db.session.commit()

    with patch.object(inbox_pwa, "send_pwa_push_notification") as push:
        recs = inbox_pwa.create_pwa_notification(
            co, event_type="incoming_sms", title="New message", body="hi",
            phone_number_id=app_ctx["line"], emit_sse=False)
    assert recs, "the notification record must still be created (history / unread preserved)"
    pushed_ids = set(push.call_args.kwargs.get("user_ids", []))
    assert app_ctx["B"] not in pushed_ids, "no real-time push for an Away user"
    assert app_ctx["A"] in pushed_ids


# ── client contract (calls.html) ─────────────────────────────────────────── #

def test_calls_template_fails_closed_while_away():
    assert "function isAway()" in CALLS_HTML
    assert "if (isAway()) return Promise.resolve(false);" in CALLS_HTML       # acquireVoiceOwnership
    assert "voiceCode: 'PHONE_AWAY'" in CALLS_HTML                            # initVoice throws
    assert "if (isAway()) { applyPhoneAvailability('away'); return; }" in CALLS_HTML  # resyncVoiceUi
    assert "data.type === 'phone_availability'" in CALLS_HTML                 # SSE apply
    assert "loadPhoneAvailability();" in CALLS_HTML
    # teardown on Away
    block = CALLS_HTML.split("function applyPhoneAvailability")[1].split("async function loadPhoneAvailability")[0]
    assert "teardownDevice();" in block and "releaseVoiceOwnership();" in block


def test_server_gates_present():
    assert 'code": "PHONE_AWAY"' in INBOX_PWA or "'code': 'PHONE_AWAY'" in INBOX_PWA or '"PHONE_AWAY"' in INBOX_PWA
    assert "available_user_ids(ta.company_id)" in TWILIO_SMS
    assert "phone_availability" in (Path(__file__).resolve().parents[1] / "models.py").read_text()
    mig = (Path(__file__).resolve().parents[1] / "migrations/20260831_phone_availability.sql").read_text().lower()
    assert "add column if not exists phone_availability" in mig
    assert " drop " not in mig and "delete from" not in mig
