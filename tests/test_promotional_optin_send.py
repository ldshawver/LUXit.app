"""Promotional opt-in — operator send flow + delivery tracking.

Covers services.promotional_optin.send_solicitation / send_solicitation_batch /
sync_solicitation_delivery_status and the POST
/api/promotional-optin/solicitations/send endpoint.

Invariants asserted here:
  * sending the opt-in *request* never mutates consent columns
  * a stale / hand-crafted contact list is re-intersected against the current
    eligible audience — STOP / suppressed / already-promotional / cross-tenant
    contacts are never contacted
  * idempotent: a solicitation already handed to Twilio is not re-sent
  * a disabled/blocked outbound path records 'blocked' and raises nothing
  * consent is still granted only by a contextual inbound YES
NO test sends a real SMS — sendConversationSms is patched, and the Twilio gate
is disabled in the testing env anyway.
"""
import os
from datetime import datetime

import pytest

from app import create_app
from extensions import db
from models import (
    Company, Contact, PromotionalConsentEvent, PromotionalOptInSolicitation,
    Segment, SegmentMember, TwilioAccount, TwilioConversation, TwilioMessage,
    User, user_company,
)


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _company(name="Promo Send Co"):
    c = Company(name=name)
    db.session.add(c)
    db.session.flush()
    return c


def _admin(company, email="promo-send-admin@example.com"):
    u = User(username=email, email=email, is_admin=True)
    u.password_hash = "x"
    db.session.add(u)
    db.session.flush()
    u.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=u.id, company_id=company.id, is_default=True))
    db.session.flush()
    return u


def _plain_user(company, email="promo-send-user@example.com"):
    u = User(username=email, email=email, is_admin=False)
    u.password_hash = "x"
    db.session.add(u)
    db.session.flush()
    u.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=u.id, company_id=company.id, is_default=True))
    db.session.flush()
    return u


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _twilio_account(company, from_phone="+15559999999"):
    account = TwilioAccount(company_id=company.id, from_phone=from_phone, is_active=True)
    account.set_account_sid("ACtest")
    account.set_auth_token("token")
    db.session.add(account)
    db.session.flush()
    return account


def _contact(company, phone, **kw):
    c = Contact(company_id=company.id, phone=phone, normalized_phone=phone, is_active=True, **kw)
    db.session.add(c)
    db.session.flush()
    return c


def _inbound_evidence(company, phone, sid):
    conv = TwilioConversation(company_id=company.id, from_number=phone, to_number="+15559999999")
    db.session.add(conv)
    db.session.flush()
    db.session.add(TwilioMessage(company_id=company.id, conversation_id=conv.id, direction="inbound",
                                 twilio_sid=sid, from_number=phone, body="hi"))
    db.session.flush()


def _segment(company, contact_ids, name="My Order Customer"):
    seg = Segment(company_id=company.id, name=name, segment_type="custom",
                  match_mode="any", is_active=True)
    db.session.add(seg)
    db.session.flush()
    for cid in contact_ids:
        db.session.add(SegmentMember(segment_id=seg.id, contact_id=cid, source="manual", is_excluded=False))
    db.session.flush()
    return seg


class _FakeSend:
    """Stand-in for twilio_sms.sendConversationSms that records calls and never
    touches a network. `result_for` maps a conversation's to/from to a canned
    provider result."""

    def __init__(self, default=None):
        self.calls = []
        self.default = default or {"success": True, "sid": "SMfake0001", "provider_status": "queued"}
        self._seq = 0

    def __call__(self, conversation_id, message, **kwargs):
        self._seq += 1
        self.calls.append({"conversation_id": conversation_id, "message": message, "kwargs": kwargs})
        res = dict(self.default)
        if res.get("success") and res.get("sid") == "SMfake0001":
            res["sid"] = f"SMfake{self._seq:04d}"
        # mimic the real function persisting an outbound TwilioMessage
        if res.get("success"):
            conv = db.session.get(TwilioConversation, conversation_id)
            db.session.add(TwilioMessage(
                company_id=conv.company_id, conversation_id=conversation_id,
                direction="outbound", twilio_sid=res["sid"],
                from_number=conv.to_number, to_number=conv.from_number,
                body=message, status="sent",
            ))
            db.session.flush()
        return res


@pytest.fixture
def patch_send(monkeypatch):
    fake = _FakeSend()
    monkeypatch.setattr("twilio_sms.sendConversationSms", fake)
    return fake


# ---------------------------------------------------------------------------

def test_send_records_sid_and_never_touches_consent(app, patch_send):
    from services.promotional_optin import send_solicitation
    co = _company()
    _twilio_account(co)
    c = _contact(co, "+14155551001")
    _inbound_evidence(co, "+14155551001", "SMv1")

    res = send_solicitation(co.id, c.id, actor_user_id=None)
    db.session.commit()

    assert res["ok"] and res["sent"] and res["resent"] is False
    row = PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=c.id).one()
    assert row.status == "pending"
    assert row.delivery_status == "queued"
    assert row.solicitation_message_sid and row.sent_at is not None
    assert len(patch_send.calls) == 1
    db.session.refresh(c)
    assert c.sms_marketing_opt_in is False
    assert c.sms_consent_status != "opted_in"


def test_send_is_idempotent_once_dispatched(app, patch_send):
    from services.promotional_optin import send_solicitation
    co = _company()
    _twilio_account(co)
    c = _contact(co, "+14155551002")
    _inbound_evidence(co, "+14155551002", "SMv2")

    send_solicitation(co.id, c.id, actor_user_id=None)
    db.session.commit()
    again = send_solicitation(co.id, c.id, actor_user_id=None)
    db.session.commit()

    assert again["sent"] and again["resent"] is False
    assert len(patch_send.calls) == 1  # no second SMS
    assert PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=c.id).count() == 1


def test_send_refuses_suppressed_and_already_promotional(app, patch_send):
    from services.promotional_optin import send_solicitation
    co = _company()
    _twilio_account(co)
    stopped = _contact(co, "+14155551003", sms_opted_out=True, sms_opt_out_at=datetime.utcnow())
    promo = _contact(co, "+14155551004", sms_marketing_opt_in=True, sms_consent_status="opted_in")
    _inbound_evidence(co, "+14155551003", "SMv3")
    _inbound_evidence(co, "+14155551004", "SMv4")

    r1 = send_solicitation(co.id, stopped.id, actor_user_id=None)
    r2 = send_solicitation(co.id, promo.id, actor_user_id=None)
    assert r1["ok"] is False and r2["ok"] is False
    assert patch_send.calls == []
    assert PromotionalOptInSolicitation.query.count() == 0


def test_blocked_send_records_blocked_and_raises_nothing(app, monkeypatch):
    from services.promotional_optin import send_solicitation
    monkeypatch.setattr("twilio_sms.sendConversationSms", lambda *a, **k: {
        "success": False,
        "error": "Twilio outbound traffic is disabled (LUXIT_TWILIO_MODE=disabled); blocked at send.",
        "error_code": "TwilioSendBlockedError",
    })
    co = _company()
    _twilio_account(co)
    c = _contact(co, "+14155551005")
    _inbound_evidence(co, "+14155551005", "SMv5")

    res = send_solicitation(co.id, c.id, actor_user_id=None)
    db.session.commit()

    assert res["ok"] and res["sent"] is False and res["blocked"] is True
    row = PromotionalOptInSolicitation.query.filter_by(company_id=co.id, contact_id=c.id).one()
    assert row.status == "pending"           # context kept for a later YES
    assert row.delivery_status == "blocked"
    assert row.send_error


def test_batch_reintersects_against_current_eligible_audience(app, patch_send):
    from services.promotional_optin import send_solicitation_batch
    co = _company()
    _twilio_account(co)
    ok1 = _contact(co, "+14155551010")
    ok2 = _contact(co, "+14155551011")
    stopped = _contact(co, "+14155551012", sms_opted_out=True, sms_opt_out_at=datetime.utcnow())
    promo = _contact(co, "+14155551013", sms_marketing_opt_in=True, sms_consent_status="opted_in")
    for p, s in [("+14155551010", "SMb1"), ("+14155551011", "SMb2"),
                 ("+14155551012", "SMb3"), ("+14155551013", "SMb4")]:
        _inbound_evidence(co, p, s)
    seg = _segment(co, [ok1.id, ok2.id, stopped.id, promo.id])

    summary = send_solicitation_batch(
        co.id, [ok1.id, ok2.id, stopped.id, promo.id, 999999],
        actor_user_id=None, segment_id=seg.id,
    )
    db.session.commit()

    assert summary["eligible"] == 2
    assert summary["sent"] == 2
    assert set(summary["skipped_not_eligible"]) == {stopped.id, promo.id, 999999}
    contacted = {call["kwargs"].get("effect_type") for call in patch_send.calls}
    assert contacted == {"promotional_optin_solicitation"}
    assert PromotionalOptInSolicitation.query.filter_by(contact_id=stopped.id).count() == 0
    assert PromotionalOptInSolicitation.query.filter_by(contact_id=promo.id).count() == 0


def test_batch_never_contacts_cross_tenant_contact(app, patch_send):
    from services.promotional_optin import send_solicitation_batch
    co_a = _company("Tenant A")
    co_b = _company("Tenant B")
    _twilio_account(co_a)
    a1 = _contact(co_a, "+14155551020")
    b1 = _contact(co_b, "+14155551021")
    _inbound_evidence(co_a, "+14155551020", "SMx1")
    _inbound_evidence(co_b, "+14155551021", "SMx2")
    seg_a = _segment(co_a, [a1.id])

    summary = send_solicitation_batch(co_a.id, [a1.id, b1.id], actor_user_id=None, segment_id=seg_a.id)
    db.session.commit()

    assert summary["sent"] == 1
    assert b1.id in summary["skipped_not_eligible"]
    assert PromotionalOptInSolicitation.query.filter_by(contact_id=b1.id).count() == 0


def test_delivery_status_callback_updates_solicitation(app, patch_send):
    from services.promotional_optin import send_solicitation, sync_solicitation_delivery_status
    co = _company()
    _twilio_account(co)
    c = _contact(co, "+14155551030")
    _inbound_evidence(co, "+14155551030", "SMv30")
    send_solicitation(co.id, c.id, actor_user_id=None)
    db.session.commit()
    row = PromotionalOptInSolicitation.query.filter_by(contact_id=c.id).one()
    sid = row.solicitation_message_sid

    assert sync_solicitation_delivery_status(sid, "delivered") == 1
    db.session.commit()
    db.session.refresh(row)
    assert row.delivery_status == "delivered"

    assert sync_solicitation_delivery_status(sid, "failed", "30008 unknown error") == 1
    db.session.commit()
    db.session.refresh(row)
    assert row.delivery_status == "failed"
    assert "30008" in (row.send_error or "")


def test_status_callback_route_syncs_solicitation(app, client, patch_send, monkeypatch):
    from services.promotional_optin import send_solicitation
    monkeypatch.setattr("twilio_sms._validate_twilio_signature", lambda *a, **k: True)
    co = _company()
    _twilio_account(co)
    c = _contact(co, "+14155551031")
    _inbound_evidence(co, "+14155551031", "SMv31")
    send_solicitation(co.id, c.id, actor_user_id=None)
    db.session.commit()
    row = PromotionalOptInSolicitation.query.filter_by(contact_id=c.id).one()
    sid = row.solicitation_message_sid

    resp = client.post("/twilio/sms/status", data={"MessageSid": sid, "MessageStatus": "delivered"})
    assert resp.status_code == 204
    db.session.refresh(row)
    assert row.delivery_status == "delivered"


def test_consent_still_only_via_contextual_yes(app, patch_send):
    from services.promotional_optin import send_solicitation, record_contextual_yes
    co = _company()
    _twilio_account(co)
    c = _contact(co, "+14155551040")
    _inbound_evidence(co, "+14155551040", "SMv40")

    send_solicitation(co.id, c.id, actor_user_id=None)
    db.session.commit()
    db.session.refresh(c)
    assert c.sms_marketing_opt_in is False   # sending alone grants nothing

    out = record_contextual_yes(co.id, "+14155551040", "+15559999999", "SMYES40", keyword="yes")
    db.session.commit()
    assert out["matched"] and out["granted"]
    db.session.refresh(c)
    assert c.sms_marketing_opt_in is True
    row = PromotionalOptInSolicitation.query.filter_by(contact_id=c.id).one()
    assert row.status == "consented"
    assert PromotionalConsentEvent.query.filter_by(contact_id=c.id).count() == 1


def test_audience_endpoint_exposes_solicitation_state(app, client, patch_send):
    from services.promotional_optin import send_solicitation
    co = _company()
    admin = _admin(co)
    _twilio_account(co)
    sent_c = _contact(co, "+14155551050")
    fresh_c = _contact(co, "+14155551051")
    _inbound_evidence(co, "+14155551050", "SMv50")
    _inbound_evidence(co, "+14155551051", "SMv51")
    seg = _segment(co, [sent_c.id, fresh_c.id])
    send_solicitation(co.id, sent_c.id, actor_user_id=admin.id)
    db.session.commit()

    _login(client, admin)
    resp = client.get(f"/api/promotional-optin/audience?segment_id={seg.id}")
    assert resp.status_code == 200
    rows = {r["contact_id"]: r for r in resp.get_json()["audience"]}
    assert rows[sent_c.id]["solicitation"]["state"] == "sent"
    assert rows[fresh_c.id]["solicitation"]["state"] == "not_sent"


def test_send_endpoint_rejects_non_admin(app, patch_send):
    co = _company()
    _admin(co)
    plain = _plain_user(co)
    _twilio_account(co)
    c = _contact(co, "+14155551060")
    _inbound_evidence(co, "+14155551060", "SMv60")
    seg = _segment(co, [c.id])
    db.session.commit()

    cl = app.test_client()
    _login(cl, plain)
    r = cl.post("/api/promotional-optin/solicitations/send",
                json={"segment_id": seg.id, "contact_ids": [c.id]})
    assert r.status_code == 403
    assert PromotionalOptInSolicitation.query.count() == 0


def test_send_endpoint_validates_and_sends(app, client, patch_send):
    co = _company()
    admin = _admin(co)
    _twilio_account(co)
    c = _contact(co, "+14155551061")
    _inbound_evidence(co, "+14155551061", "SMv61")
    seg = _segment(co, [c.id])
    db.session.commit()

    _login(client, admin)
    r = client.post("/api/promotional-optin/solicitations/send", json={"segment_id": seg.id, "contact_ids": []})
    assert r.status_code == 400

    r = client.post("/api/promotional-optin/solicitations/send",
                    json={"segment_id": seg.id, "contact_ids": [c.id]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] and body["sent"] == 1
    db.session.refresh(c)
    assert c.sms_marketing_opt_in is False


def test_send_endpoint_drops_cross_tenant_contact(app, client, patch_send):
    co_a = _company("Send Tenant A")
    co_b = _company("Send Tenant B")
    admin_a = _admin(co_a, "send-a-admin@example.com")
    _twilio_account(co_a)
    a1 = _contact(co_a, "+14155551070")
    b1 = _contact(co_b, "+14155551071")
    _inbound_evidence(co_a, "+14155551070", "SMv70")
    _inbound_evidence(co_b, "+14155551071", "SMv71")
    seg_a = _segment(co_a, [a1.id])

    _login(client, admin_a)
    r = client.post("/api/promotional-optin/solicitations/send",
                    json={"segment_id": seg_a.id, "contact_ids": [a1.id, b1.id]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["sent"] == 1
    assert b1.id in body["skipped_not_eligible"]
    assert PromotionalOptInSolicitation.query.filter_by(contact_id=b1.id).count() == 0
