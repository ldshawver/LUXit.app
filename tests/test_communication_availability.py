"""Unified Communication Availability — Available / Away for shared calls AND texts.

Extends the accepted Phone Availability work (tests/test_phone_availability.py,
tests/test_phone_availability_acceptance.py) with the refinement that AWAY now
provably suppresses EVERY user-facing realtime layer for shared SMS/chat as well
as Voice — the SSE attention stream, push, badges, sounds — while inbound
persistence, canonical resolution, MessageSid idempotency, STOP/compliance,
automation and customer consent are all untouched.

Targeted matrix (spec section 18) — item numbers referenced in each test.
"""
import json
import os
import queue as _queue
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
from models import (Company, Notification, PhoneNumberUserPermission, PWADevice,
                    TwilioAccount, TwilioPhoneNumber, User, UserCompanyAccess)
from services.phone_availability import (available_user_ids, is_available,
                                         set_all_availability, set_availability)

ROOT = Path(__file__).resolve().parents[1]
INBOX_PWA = (ROOT / "inbox_pwa.py").read_text()
INDEX_HTML = (ROOT / "templates/inbox_pwa/index.html").read_text()
CALLS_HTML = (ROOT / "templates/inbox_pwa/calls.html").read_text()
ADMIN_HTML = (ROOT / "templates/admin/communications.html").read_text()
SVC = (ROOT / "services/phone_availability.py").read_text()


@pytest.fixture
def ctx():
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
                                 voice_enabled=True, is_active=True,
                                 during_hours_route="ring_pwa", after_hours_route="voicemail",
                                 voicemail_greeting_text="Please leave a message.")
        db.session.add(line); db.session.flush()

        def mk(name, cid, role="staff", admin=False, manage=False):
            u = User(username=name, email=f"{name}@x.com",
                     password_hash=generate_password_hash("pw"), is_admin=admin,
                     default_company_id=cid)
            db.session.add(u); db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=cid, role=role,
                                             is_default=True, pwa_access_enabled=True,
                                             can_access_full_app=True, manage_users_enabled=manage))
            db.session.add(PWADevice(company_id=cid, user_id=u.id, device_key=f"dev-{name}",
                                     approved_status="approved", lifecycle_status="active"))
            if cid == co.id:
                db.session.add(PhoneNumberUserPermission(
                    company_id=cid, user_id=u.id, phone_number_id=line.id,
                    can_access_pwa=True, can_view_sms=True, can_view_calls=True,
                    can_view_voicemail=True, can_call=True))
            return u

        A = mk("alice", co.id, role="owner")
        B = mk("bob", co.id)
        admin = mk("dave", co.id, role="admin", manage=True)
        outsider = mk("mallory", other.id, role="admin", admin=True, manage=True)
        db.session.commit()
        yield dict(app=app, co=co.id, other=other.id, line=line.id,
                   A=A.id, B=B.id, admin=admin.id, outsider=outsider.id)
        db.session.remove(); db.drop_all()


class _Conv:
    def __init__(self, line_id):
        self.id = 4242
        self.contact_name = "A Customer"
        self.from_number = "+14155551212"
        self.phone_number_id = line_id
        self.company_id = None
        self.last_message_at = None
        self.last_message_preview = ""


def _login(client, uid):
    with client.session_transaction() as s:
        s["_user_id"] = str(uid); s["_fresh"] = True; s["user_id"] = uid; s["logged_in"] = True


def _fire_sms(ctx, body="hi"):
    import inbox_pwa
    return inbox_pwa._fire_push_notification(ctx["co"], _Conv(ctx["line"]), body)


# ── 1 / 2 : calls eligible when Available, suppressed when Away ──────────────

def test_calls_eligible_available_suppressed_away(ctx):
    c = ctx["app"].test_client(); _login(c, ctx["B"])
    assert is_available(ctx["B"], ctx["co"]) is True
    set_availability(ctx["B"], ctx["co"], "away", actor_user_id=ctx["B"], source="user")
    db.session.commit()
    r = c.get("/api/phone/voice-token")
    assert r.status_code == 403 and r.get_json()["code"] == "PHONE_AWAY"
    set_availability(ctx["B"], ctx["co"], "available", actor_user_id=ctx["B"], source="user")
    db.session.commit()
    r2 = c.get("/api/phone/voice-token")
    assert r2.status_code != 403 or r2.get_json().get("code") != "PHONE_AWAY"


# ── 3 / 4 / 5 : SMS notification eligible / suppressed / persists while Away ──

def test_sms_notification_suppressed_but_persists_while_away(ctx):
    import inbox_pwa
    set_availability(ctx["B"], ctx["co"], "away", actor_user_id=ctx["B"], source="user")
    db.session.commit()
    with patch.object(inbox_pwa, "send_pwa_push_notification") as push:
        _fire_sms(ctx, "hello")
    pushed = set(push.call_args.kwargs.get("user_ids", []))
    assert ctx["B"] not in pushed and ctx["A"] in pushed          # 4
    rows = {r.user_id: r for r in Notification.query.filter_by(
        company_id=ctx["co"], event_type="incoming_sms").all()}
    assert ctx["B"] in rows and rows[ctx["B"]].is_read is False   # 5 / 9
    assert ctx["A"] in rows and rows[ctx["A"]].is_read is False


# ── 6 / 7 / 8 : canonical history preserved, full on return, no replay storm ──

def test_return_available_no_replay_and_history_intact(ctx):
    import inbox_pwa
    set_availability(ctx["B"], ctx["co"], "away", actor_user_id=ctx["B"], source="user")
    db.session.commit()
    with patch.object(inbox_pwa, "send_pwa_push_notification"):
        _fire_sms(ctx, "m1"); _fire_sms(ctx, "m2"); _fire_sms(ctx, "m3")
    # B's shared history is fully intact (same rows as an Available user).
    b_rows = Notification.query.filter_by(company_id=ctx["co"], user_id=ctx["B"],
                                          event_type="incoming_sms").count()
    a_rows = Notification.query.filter_by(company_id=ctx["co"], user_id=ctx["A"],
                                          event_type="incoming_sms").count()
    assert b_rows == a_rows == 3
    with patch.object(inbox_pwa, "send_pwa_push_notification") as push, \
         patch.object(inbox_pwa, "_push_sse_event") as sse:
        c = ctx["app"].test_client(); _login(c, ctx["B"])
        r = c.put("/api/phone/availability", json={"state": "available"})
    assert r.status_code == 200
    assert push.call_count == 0                                   # 8 no replay storm
    assert sse.call_count == 1 and sse.call_args.args[1] == "phone_availability"


# ── 10 / 11 / 12 : SSE attention events suppressed for an Away listener ──────

def _register_listener(company_id, user_id):
    import inbox_pwa
    q = _queue.Queue(maxsize=100)
    with inbox_pwa._sse_lock:
        inbox_pwa._sse_listeners.setdefault(company_id, []).append((user_id, q))
    return q


def _drain(q):
    out = []
    try:
        while True:
            out.append(json.loads(q.get_nowait()))
    except _queue.Empty:
        pass
    return out


def test_sse_attention_suppressed_for_away_listener(ctx):
    import inbox_pwa
    inbox_pwa._sse_listeners.clear()
    qa = _register_listener(ctx["co"], ctx["A"])
    qb = _register_listener(ctx["co"], ctx["B"])
    set_availability(ctx["B"], ctx["co"], "away", actor_user_id=ctx["B"], source="user")
    db.session.commit()
    try:
        for ev in ("new_message", "incoming_call", "voicemail", "unread_message_reminder"):
            inbox_pwa._push_sse_event(ctx["co"], ev, {"x": 1})
        a_types = {e["type"] for e in _drain(qa)}
        b_types = {e["type"] for e in _drain(qb)}
        assert a_types == {"new_message", "incoming_call", "voicemail", "unread_message_reminder"}
        assert b_types == set(), "an Away listener must receive no realtime attention events"
        # non-attention events still reach the Away listener (state convergence)
        inbox_pwa._push_sse_event(ctx["co"], "phone_availability", {"user_id": ctx["B"], "state": "away"})
        inbox_pwa._push_sse_event(ctx["co"], "message_status", {"conversation_id": 1})
        b_types2 = {e["type"] for e in _drain(qb)}
        assert b_types2 == {"phone_availability", "message_status"}
    finally:
        inbox_pwa._sse_listeners.clear()


def test_sse_attention_restored_after_return_available(ctx):
    import inbox_pwa
    inbox_pwa._sse_listeners.clear()
    qb = _register_listener(ctx["co"], ctx["B"])
    set_availability(ctx["B"], ctx["co"], "away", actor_user_id=ctx["B"], source="user")
    db.session.commit()
    try:
        inbox_pwa._push_sse_event(ctx["co"], "new_message", {"n": 1})
        assert _drain(qb) == []
        set_availability(ctx["B"], ctx["co"], "available", actor_user_id=ctx["B"], source="user")
        db.session.commit()
        inbox_pwa._push_sse_event(ctx["co"], "new_message", {"n": 2})
        assert [e["n"] for e in _drain(qb)] == [2]
    finally:
        inbox_pwa._sse_listeners.clear()


# ── 13 : multi-device — one authoritative row, not device-scoped ────────────

def test_state_is_user_scoped_not_device_scoped(ctx):
    # Two approved devices for B; going Away is a single UCA row → both devices.
    db.session.add(PWADevice(company_id=ctx["co"], user_id=ctx["B"], device_key="dev-bob-2",
                             approved_status="approved", lifecycle_status="active"))
    db.session.commit()
    set_availability(ctx["B"], ctx["co"], "away", actor_user_id=ctx["B"], source="user")
    db.session.commit()
    rows = UserCompanyAccess.query.filter_by(user_id=ctx["B"], company_id=ctx["co"]).all()
    assert len(rows) == 1 and rows[0].phone_availability == "away"
    assert is_available(ctx["B"], ctx["co"]) is False


# ── 14 / 15 / 16 : admin individual control + view ──────────────────────────

def test_admin_sets_and_views_individual(ctx):
    c = ctx["app"].test_client(); _login(c, ctx["admin"])
    assert c.put(f"/api/phone/availability/user/{ctx['B']}", json={"state": "away"}).status_code == 200
    assert is_available(ctx["B"], ctx["co"]) is False
    team = c.get("/api/phone/availability/team").get_json()["team"]
    bob = next(r for r in team if r["user_id"] == ctx["B"])
    assert bob["state"] == "away" and bob["source"] == "admin"
    assert bob["changed_by_name"] == "dave" and bob["changed_at"]
    assert c.put(f"/api/phone/availability/user/{ctx['B']}", json={"state": "available"}).status_code == 200
    assert is_available(ctx["B"], ctx["co"]) is True


# ── 17 : admin bulk Set All Available ──────────────────────────────────────

def test_admin_bulk_set_all_available(ctx):
    for uid in (ctx["A"], ctx["B"], ctx["admin"]):
        set_availability(uid, ctx["co"], "away", actor_user_id=ctx["admin"], source="admin")
    db.session.commit()
    c = ctx["app"].test_client(); _login(c, ctx["admin"])
    r = c.put("/api/phone/availability/team", json={"state": "available"})
    assert r.status_code == 200 and r.get_json()["updated"] == 3
    assert available_user_ids(ctx["co"]) == {ctx["A"], ctx["B"], ctx["admin"]}


# ── 18 : admin bulk Set All Away requires deliberate confirmation ──────────

def test_admin_bulk_set_all_away_requires_confirm(ctx):
    c = ctx["app"].test_client(); _login(c, ctx["admin"])
    r = c.put("/api/phone/availability/team", json={"state": "away"})
    assert r.status_code == 409 and r.get_json()["code"] == "CONFIRM_REQUIRED"
    assert available_user_ids(ctx["co"]) == {ctx["A"], ctx["B"], ctx["admin"]}, "no change without confirm"
    r2 = c.put("/api/phone/availability/team", json={"state": "away", "confirm": True})
    assert r2.status_code == 200 and r2.get_json()["updated"] == 3
    assert available_user_ids(ctx["co"]) == set()


# ── 19 / 20 : authz — ordinary user + cross-tenant ────────────────────────

def test_ordinary_user_cannot_mutate_another(ctx):
    c = ctx["app"].test_client(); _login(c, ctx["B"])
    assert c.put(f"/api/phone/availability/user/{ctx['A']}", json={"state": "away"}).status_code == 403
    assert c.put("/api/phone/availability/team", json={"state": "available"}).status_code == 403
    assert is_available(ctx["A"], ctx["co"]) is True


def test_cross_tenant_admin_cannot_mutate(ctx):
    c = ctx["app"].test_client(); _login(c, ctx["outsider"])
    # outsider is a platform admin of Other Co; the target lives in Shared Line Co.
    r = c.put(f"/api/phone/availability/user/{ctx['B']}", json={"state": "away"})
    assert r.status_code in (403, 404)
    assert is_available(ctx["B"], ctx["co"]) is True


# ── 21 / 22 : all users Away — SMS still persists, no webhook error ────────

def test_all_users_away_sms_persists_no_error(ctx):
    import inbox_pwa
    set_all_availability(ctx["co"], "away", actor_user_id=ctx["admin"], source="admin")
    db.session.commit()
    assert available_user_ids(ctx["co"]) == set()
    with patch.object(inbox_pwa, "send_pwa_push_notification") as push:
        rows = _fire_sms(ctx, "nobody home")  # must not raise
    assert push.call_args.kwargs.get("user_ids") == []
    persisted = Notification.query.filter_by(company_id=ctx["co"], event_type="incoming_sms").count()
    assert persisted >= 3, "the inbound SMS notification still persists for every authorized user"


def test_all_users_away_call_fallback_is_voicemail(ctx):
    # The ring block already falls back to voicemail when every approved device
    # is Away — assert the wiring is present (behaviour proven in
    # test_phone_availability.py::*ring*).
    assert "_ring_pwa_twiml_done" in (ROOT / "twilio_sms.py").read_text()
    assert "available_user_ids" in (ROOT / "twilio_sms.py").read_text()


# ── 23 : STOP / compliance unaffected ────────────────────────────────────

def test_stop_compliance_never_consults_availability(ctx):
    twilio = (ROOT / "twilio_sms.py").read_text()
    kw_region = twilio[twilio.index("def inbound_sms():"):twilio.index("def _inbound_call_impl():")]
    assert "phone_availability" not in kw_region and "available_user_ids" not in kw_region
    for token in ("is_opted_out", "consent_status", "TwilioConversation", "mark_opt_out"):
        assert token not in SVC, f"availability service must never reference {token}"


# ── 24 : promotional opt-in semantics unaffected ─────────────────────────

def test_promotional_optin_service_has_no_availability_coupling(ctx):
    promo = (ROOT / "services/promotional_optin.py").read_text()
    assert "phone_availability" not in promo and "available_user_ids" not in promo
    assert "communication_availability" not in promo.lower()


# ── 25 / 26 : frozen Voice + client contract, campaign/segment untouched ──

def test_client_guards_are_present(ctx):
    # calls.html — Voice integration contract unchanged + label refined.
    assert "data.type === 'incoming_call' && !isAway()" in CALLS_HTML
    assert "Communication Availability" in CALLS_HTML
    # index.html — SMS inbox honours Away at every attention layer.
    assert "isCommsAway()" in INDEX_HTML
    assert "_ATTENTION_SSE" in INDEX_HTML
    assert "applyCommsAvailability" in INDEX_HTML
    assert "if (isCommsAway()) return;" in INDEX_HTML  # notif-prompt guard
    assert "commsAwayBanner" in INDEX_HTML
    # admin UI — table + bulk controls with confirmation.
    assert "availSetAllAway" in ADMIN_HTML and "Set all users Away?" in ADMIN_HTML
    assert "/api/phone/availability/team" in ADMIN_HTML


def test_server_attention_event_set_matches_client(ctx):
    # The server attention list and the client guard list must agree.
    assert "_ATTENTION_SSE_EVENTS" in INBOX_PWA
    for ev in ("new_message", "incoming_call", "missed_call", "voicemail", "unread_message_reminder"):
        assert ev in INBOX_PWA.split("_ATTENTION_SSE_EVENTS")[1].split("}")[0]
