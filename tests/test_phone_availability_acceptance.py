"""Shared Phone User Availability — consolidated acceptance.

Complements tests/test_phone_availability.py. Proves the acceptance points that
the first suite did not cover directly:

  6/7  AWAY suppresses the inbound-SMS Web Push AND the device badge, on the
       real inbound-SMS notification path (_fire_push_notification), not only
       the call/voicemail path (create_pwa_notification).
  8/9  While AWAY the inbound SMS still persists a notification record and the
       server-side unread state stays intact (nothing is marked read).
  10   Returning Available restores future push.
  11   Returning Available does NOT replay historical notifications (the
       set_availability path emits no push at all).
  13   An authorized admin can restore a user to Available.
  16   A change (self or admin) broadcasts the realtime 'phone_availability'
       SSE event so multi-device clients converge without polling.
  18   STOP / compliance keyword handling is independent of availability.
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
from models import (Company, Notification, PhoneNumberUserPermission, PWADevice,
                    TwilioAccount, TwilioPhoneNumber, User, UserCompanyAccess)
from services.phone_availability import is_available, set_availability

INBOX_PWA = (Path(__file__).resolve().parents[1] / "inbox_pwa.py").read_text()
TWILIO_SMS = (Path(__file__).resolve().parents[1] / "twilio_sms.py").read_text()


@pytest.fixture
def ctx():
    app = create_app()
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
                      WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        co = Company(name="Shared Line Co", is_active=True)
        db.session.add(co); db.session.flush()
        acct = TwilioAccount(company_id=co.id, from_phone="+19165989519", is_active=True)
        db.session.add(acct); db.session.flush()
        line = TwilioPhoneNumber(company_id=co.id, twilio_account_id=acct.id,
                                 phone_number="+19165989519", friendly_name="Shared",
                                 voice_enabled=True, is_active=True,
                                 during_hours_route="ring_pwa", after_hours_route="voicemail",
                                 voicemail_greeting_text="Please leave a message.")
        db.session.add(line); db.session.flush()

        def mk(name, role="staff", manage=False):
            u = User(username=name, email=f"{name}@x.com",
                     password_hash=generate_password_hash("pw"), default_company_id=co.id)
            db.session.add(u); db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role=role,
                                             is_default=True, pwa_access_enabled=True,
                                             can_access_full_app=True, manage_users_enabled=manage))
            db.session.add(PWADevice(company_id=co.id, user_id=u.id, device_key=f"dev-{name}",
                                     approved_status="approved", lifecycle_status="active"))
            # every user is an authorized recipient of the shared line
            db.session.add(PhoneNumberUserPermission(
                company_id=co.id, user_id=u.id, phone_number_id=line.id,
                can_access_pwa=True, can_view_sms=True, can_view_calls=True,
                can_view_voicemail=True, can_call=True))
            return u

        A = mk("alice", role="owner")
        B = mk("bob")
        admin = mk("dave", role="admin", manage=True)
        db.session.commit()
        yield dict(app=app, co=co.id, line=line.id, A=A.id, B=B.id, admin=admin.id)
        db.session.remove(); db.drop_all()


class _Conv:
    """Minimal stand-in for a TwilioConversation for _fire_push_notification."""
    def __init__(self, line_id):
        self.id = 4242
        self.contact_name = "A Customer"
        self.from_number = "+14155551212"
        self.phone_number_id = line_id
        self.last_message_at = None
        self.last_message_preview = ""


def _login(client, uid):
    with client.session_transaction() as s:
        s["_user_id"] = str(uid); s["_fresh"] = True; s["user_id"] = uid; s["logged_in"] = True


def _fire_sms(ctx, body="hi"):
    import inbox_pwa
    return inbox_pwa._fire_push_notification(ctx["co"], _Conv(ctx["line"]), body)


# ── 6/7 : inbound-SMS push + badge suppressed while Away ─────────────────────

def test_inbound_sms_push_suppressed_for_away_user(ctx):
    import inbox_pwa
    set_availability(ctx["B"], ctx["co"], "away", actor_user_id=ctx["B"], source="user")
    db.session.commit()

    with patch.object(inbox_pwa, "send_pwa_push_notification") as push:
        _fire_sms(ctx, "hello there")

    pushed = set(push.call_args.kwargs.get("user_ids", []))
    assert ctx["B"] not in pushed, "an Away user must get no inbound-SMS push / badge"
    assert ctx["A"] in pushed, "an Available user must still be pushed"


def test_both_notification_paths_share_the_availability_filter(ctx):
    # The call/voicemail path AND the inbound-SMS path must both gate on
    # availability — not just one of them.
    sms_frag = INBOX_PWA.split("def _fire_push_notification")[1].split("def create_pwa_notification")[0]
    call_frag = INBOX_PWA.split("def create_pwa_notification")[1].split("def create_unread_message_reminders")[0]
    assert "available_user_ids" in sms_frag
    assert "available_user_ids" in call_frag


# ── 8/9 : SMS persists + unread state intact while Away ─────────────────────

def test_inbound_sms_record_persists_and_stays_unread_while_away(ctx):
    import inbox_pwa
    set_availability(ctx["B"], ctx["co"], "away", actor_user_id=ctx["B"], source="user")
    db.session.commit()

    with patch.object(inbox_pwa, "send_pwa_push_notification"):
        _fire_sms(ctx, "still here")

    rows = Notification.query.filter_by(company_id=ctx["co"], event_type="incoming_sms").all()
    by_user = {r.user_id: r for r in rows}
    assert ctx["B"] in by_user, "the Away user's notification record must still be written"
    assert by_user[ctx["B"]].is_read is False, "server-side unread state must be untouched by Away"
    assert ctx["A"] in by_user and by_user[ctx["A"]].is_read is False


def test_away_does_not_change_unread_counts(ctx):
    import inbox_pwa
    set_availability(ctx["B"], ctx["co"], "away", actor_user_id=ctx["B"], source="user")
    db.session.commit()
    with patch.object(inbox_pwa, "send_pwa_push_notification"):
        _fire_sms(ctx, "one"); _fire_sms(ctx, "two")
    unread_b = Notification.query.filter_by(company_id=ctx["co"], user_id=ctx["B"], is_read=False).count()
    unread_a = Notification.query.filter_by(company_id=ctx["co"], user_id=ctx["A"], is_read=False).count()
    assert unread_b == 2 and unread_a == 2, "unread state identical for Away and Available users"


# ── 10 / 11 : return-to-Available restores future push, replays nothing ─────

def test_returning_available_restores_push_and_replays_nothing(ctx):
    import inbox_pwa
    set_availability(ctx["B"], ctx["co"], "away", actor_user_id=ctx["B"], source="user")
    db.session.commit()

    with patch.object(inbox_pwa, "send_pwa_push_notification"):
        _fire_sms(ctx, "while away")

    with patch.object(inbox_pwa, "send_pwa_push_notification") as push, \
         patch.object(inbox_pwa, "_push_sse_event") as sse:
        client = ctx["app"].test_client(); _login(client, ctx["B"])
        r = client.put("/api/phone/availability", json={"state": "available"})
    assert r.status_code == 200 and r.get_json()["state"] == "available"
    assert push.call_count == 0, "returning Available must not replay past notifications"
    assert sse.call_count == 1 and sse.call_args.args[1] == "phone_availability"

    with patch.object(inbox_pwa, "send_pwa_push_notification") as push2:
        _fire_sms(ctx, "after return")
    assert ctx["B"] in set(push2.call_args.kwargs.get("user_ids", []))


# ── 13 : admin restores Available ─────────────────────────────────────────

def test_admin_can_restore_available(ctx):
    client = ctx["app"].test_client(); _login(client, ctx["admin"])
    a = client.put(f"/api/phone/availability/user/{ctx['B']}", json={"state": "away"})
    assert a.status_code == 200, a.get_data(as_text=True)
    assert is_available(ctx["B"], ctx["co"]) is False
    r = client.put(f"/api/phone/availability/user/{ctx['B']}", json={"state": "available"})
    assert r.status_code == 200 and r.get_json()["state"] == "available"
    assert r.get_json()["source"] == "admin"
    assert is_available(ctx["B"], ctx["co"]) is True


# ── 16 : realtime broadcast for multi-device convergence ──────────────────

def test_self_change_broadcasts_sse(ctx):
    import inbox_pwa
    with patch.object(inbox_pwa, "_push_sse_event") as sse:
        c = ctx["app"].test_client(); _login(c, ctx["B"])
        r = c.put("/api/phone/availability", json={"state": "away"})
    assert r.status_code == 200
    assert sse.call_count == 1 and sse.call_args.args[1] == "phone_availability"
    payload = sse.call_args.args[2]
    assert payload["user_id"] == ctx["B"] and payload["state"] == "away"


def test_admin_change_broadcasts_sse(ctx):
    import inbox_pwa
    with patch.object(inbox_pwa, "_push_sse_event") as sse:
        c = ctx["app"].test_client(); _login(c, ctx["admin"])
        r = c.put(f"/api/phone/availability/user/{ctx['B']}", json={"state": "away"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert sse.call_count == 1 and sse.call_args.args[1] == "phone_availability"
    payload = sse.call_args.args[2]
    assert payload["user_id"] == ctx["B"] and payload["source"] == "admin"


# ── 18 : STOP / compliance is independent of availability ─────────────────

def test_stop_compliance_path_never_consults_availability(ctx):
    # Every availability reference in twilio_sms.py lives inside _inbound_call_impl
    # (the voice ring block) — never in the STOP/START/HELP keyword path, which
    # runs earlier in inbound_sms().
    fn_start = TWILIO_SMS.index("def _inbound_call_impl():")
    for marker in ("phone_availability", "available_user_ids"):
        assert marker in TWILIO_SMS, marker
        first = TWILIO_SMS.index(marker)
        assert first > fn_start, f"{marker} leaked outside the voice ring block"
    # the inbound-SMS + keyword region carries no availability reference at all
    kw_region = TWILIO_SMS[TWILIO_SMS.index("def inbound_sms():"):fn_start]
    assert "phone_availability" not in kw_region and "available_user_ids" not in kw_region


def test_client_suppresses_incoming_call_ui_and_sound_while_away(ctx):
    # Point 5: an AWAY client must not paint the incoming-call UI or play the
    # ring — the SSE incoming_call handler is guarded by !isAway().
    html = (Path(__file__).resolve().parents[1] / "templates/inbox_pwa/calls.html").read_text()
    assert "data.type === 'incoming_call' && !isAway()" in html
    # and the enable path is inert while Away
    assert 'enableWifiCalling() { if (isAway())' in html


def test_availability_service_only_reads_membership_not_consent(ctx):
    # The service resolves state from user_company_access only; it never reads
    # conversation opt-out / consent state, so STOP/compliance cannot be
    # entangled with Away.
    svc = (Path(__file__).resolve().parents[1] / "services/phone_availability.py")
    tree = svc.read_text()
    for token in ("is_opted_out", "opted_out", "TwilioConversation", "Contact.query",
                  "consent_status", "mark_opt_out"):
        assert token not in tree, f"availability service must not reference {token}"
