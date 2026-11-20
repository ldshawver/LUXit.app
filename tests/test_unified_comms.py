"""Unified communications (M4): identities/routing, consent-enforcing send,
email history, caller context. No provider is ever called."""
import uuid
from datetime import datetime

import pytest
from flask import g
from flask.testing import FlaskClient

from app import create_app
from extensions import db
from models import (
    BusinessIdentity, Company, Contact, ContactEmailAddress, EmailMessage, TwilioConversation, TwilioPhoneNumber,
    User, UserCompanyAccess,
)
from services import unified_comms as comms
from services.contact_timeline import build_timeline

LINE_1, LINE_2, LINE_B = "+19165550111", "+19165550112", "+19165550113"
CUSTOMER = "+19168675341"


class _FreshUserClient(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@pytest.fixture
def env(monkeypatch):
    sent = []

    def fake_send(**kwargs):
        sent.append(kwargs)
        return {"success": True, "sid": "SM123", "status": "sent", "idempotent": False}

    import services.sms_send_idempotency as idem
    monkeypatch.setattr(idem, "send_with_idempotency", fake_send)
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    app.test_client_class = _FreshUserClient
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        a, b = Company(name=f"UC A {tag}"), Company(name=f"UC B {tag}")
        db.session.add_all([a, b])
        db.session.flush()
        ids = {"a": a.id, "b": b.id}
        for key, company, role in (("admin", a, "admin"), ("editor", a, "editor"), ("viewer", a, "viewer"),
                                   ("other", b, "owner")):
            u = User(username=f"uc-{key}-{tag}", email=f"uc-{key}-{tag}@example.com", password_hash="x",
                     default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            ids[key] = u.id
        n1 = TwilioPhoneNumber(company_id=a.id, phone_number=LINE_1, friendly_name="Main")
        n2 = TwilioPhoneNumber(company_id=a.id, phone_number=LINE_2, friendly_name="Service")
        nb = TwilioPhoneNumber(company_id=b.id, phone_number=LINE_B, friendly_name="B")
        db.session.add_all([n1, n2, nb])
        c = Contact(company_id=a.id, first_name="Sam", phone=CUSTOMER, email=f"sam-{tag}@example.com")
        db.session.add(c)
        db.session.flush()
        db.session.add(ContactEmailAddress(company_id=a.id, contact_id=c.id, original_value=f"sam.work-{tag}@example.com",
                                           normalized_value=f"sam.work-{tag}@example.com"))
        conv1 = TwilioConversation(company_id=a.id, contact_id=c.id, from_number=CUSTOMER, to_number=LINE_1)
        db.session.add(conv1)
        db.session.commit()
        ids.update(n1=n1.id, n2=n2.id, nb=nb.id, contact=c.id, conv1=conv1.id, tag=tag)
        yield app, ids, sent
        db.session.remove()


def _as(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def _identity(company_id, name, number_ids, default=False, address=None):
    i = comms.save_identity(company_id, {"name": name, "number_ids": number_ids, "is_default": default,
                                         "email_from_address": address})
    db.session.commit()
    return i


def test_identity_admin_routing_and_ownership(env):
    app, ids, _ = env
    admin = _as(app, ids["admin"])
    r = admin.post("/api/comms/identities", json={"name": "Main Street Plumbing", "number_ids": [ids["n1"]],
                                                  "is_default": True, "email_from_address": "Hello@Main.example"})
    assert r.status_code == 201
    ident = r.get_json()["identity"]
    assert ident["email_from_address"] == "hello@main.example" and ident["numbers"][0]["phone_number"] == LINE_1
    assert admin.post("/api/comms/identities", json={"name": "Steal", "number_ids": [ids["nb"]]}).status_code == 400
    assert admin.post("/api/comms/identities", json={"name": "Dup", "number_ids": [ids["n1"]]}).status_code == 409
    assert _as(app, ids["editor"]).post("/api/comms/identities", json={"name": "X"}).status_code == 403
    service = _identity(ids["a"], "Service Dept", [ids["n2"]], default=True)
    assert comms.identity_for_number(ids["a"], LINE_2).id == service.id
    assert comms.identity_for_number(ids["a"], LINE_1).name == "Main Street Plumbing"
    assert comms.identity_for_number(ids["a"], "+19165559999").id == service.id  # default
    assert BusinessIdentity.query.filter_by(company_id=ids["a"], is_default=True).count() == 1
    assert comms.identity_for_number(ids["b"], LINE_1) is None
    listed = _as(app, ids["other"]).get("/api/comms/identities").get_json()["identities"]
    assert listed == []


def test_sms_send_uses_identity_thread_and_idempotent_path(env):
    app, ids, sent = env
    main = _identity(ids["a"], "Main", [ids["n1"]])
    service = _identity(ids["a"], "Service", [ids["n2"]])
    c = _as(app, ids["editor"])
    r = c.post(f"/api/contacts/{ids['contact']}/send", json={"channel": "sms", "body": "On our way",
                                                             "identity_id": main.id},
               headers={"Idempotency-Key": "k1"})
    assert r.status_code == 200 and r.get_json()["conversation_id"] == ids["conv1"]
    assert sent[0]["conversation_id"] == ids["conv1"] and sent[0]["idempotency_key"] == "k1"
    r = c.post(f"/api/contacts/{ids['contact']}/send", json={"channel": "sms", "body": "Hi", "identity_id": service.id})
    assert r.status_code == 409 and r.get_json()["code"] == "no_conversation"
    assert len(sent) == 1
    assert db.session.get(Contact, ids["contact"]).last_contacted_at is not None


@pytest.mark.parametrize("setup,payload,code", [
    (lambda c: setattr(c, "do_not_contact", True), {"channel": "sms", "body": "x"}, "do_not_contact"),
    (lambda c: setattr(c, "sms_opted_out", True), {"channel": "sms", "body": "x"}, "sms_opted_out"),
    (lambda c: None, {"channel": "sms", "body": "x", "purpose": "marketing"}, "marketing_not_allowed"),
    (lambda c: None, {"channel": "sms", "body": "  "}, "invalid"),
    (lambda c: None, {"channel": "fax", "body": "x"}, "invalid"),
    (lambda c: None, {"channel": "email", "body": "x"}, "not_configured"),
])
def test_send_refusals_never_reach_provider(env, setup, payload, code):
    app, ids, sent = env
    setup(db.session.get(Contact, ids["contact"]))
    db.session.commit()
    r = _as(app, ids["editor"]).post(f"/api/contacts/{ids['contact']}/send", json=payload)
    assert r.get_json()["code"] == code and r.status_code in (400, 409, 422)
    assert sent == []


def test_send_permissions_and_isolation(env):
    app, ids, sent = env
    body = {"channel": "sms", "body": "x"}
    assert _as(app, ids["viewer"]).post(f"/api/contacts/{ids['contact']}/send", json=body).status_code == 403
    assert _as(app, ids["other"]).post(f"/api/contacts/{ids['contact']}/send", json=body).status_code == 404
    assert sent == []


def test_email_send_when_enabled_records_history(env, monkeypatch):
    app, ids, _ = env
    monkeypatch.setenv("LUXIT_TENANT_EMAIL_SEND_ENABLED", "1")
    mails = []
    import email_service
    monkeypatch.setattr(email_service.EmailService, "send_email",
                        lambda self, to, subject, html, from_email=None: mails.append((to, subject, html, from_email)) or True)
    _identity(ids["a"], "Main", [ids["n1"]], default=True, address="hello@main.example")
    r = _as(app, ids["editor"]).post(f"/api/contacts/{ids['contact']}/send",
                                     json={"channel": "email", "body": "Quote attached <b>", "subject": "Quote"})
    assert r.status_code == 200 and r.get_json()["status"] == "sent"
    to, subject, html, sender = mails[0]
    assert sender == "hello@main.example" and "&lt;b&gt;" in html and subject == "Quote"
    tl = build_timeline(ids["a"], ids["contact"], kinds=["email"])
    assert tl["entries"][0]["title"] == "Email sent: Quote"


def test_record_email_links_secondary_address_and_is_idempotent(env):
    app, ids, _ = env
    tag = ids["tag"]
    msg, created = comms.record_email(ids["a"], direction="inbound", from_address=f"Sam <sam.work-{tag}@example.com>",
                                      to_addresses=["hello@main.example"], subject="Question", body_text="Can you call?",
                                      provider="graph", provider_message_id="AAMk-1")
    assert created and msg.contact_id == ids["contact"]
    again, created = comms.record_email(ids["a"], direction="inbound", from_address="x@example.com",
                                        to_addresses=[], provider="graph", provider_message_id="AAMk-1")
    assert not created and again.id == msg.id
    stranger, _ = comms.record_email(ids["a"], direction="inbound", from_address="nobody@example.com",
                                     to_addresses=["hello@main.example"], provider="graph", provider_message_id="AAMk-2")
    assert stranger.contact_id is None
    other, _ = comms.record_email(ids["b"], direction="inbound", from_address=f"sam.work-{tag}@example.com",
                                  to_addresses=[], provider="graph", provider_message_id="AAMk-1")
    assert other.contact_id is None and other.id != msg.id
    db.session.commit()
    assert EmailMessage.query.filter_by(company_id=ids["a"]).count() == 2


def test_logged_email_appears_on_timeline(env):
    app, ids, _ = env
    r = _as(app, ids["editor"]).post(f"/api/contacts/{ids['contact']}/emails",
                                     json={"direction": "inbound", "subject": "Re: quote", "body": "Looks good"})
    assert r.status_code == 201
    entry = build_timeline(ids["a"], ids["contact"], kinds=["email"])["entries"][0]
    assert entry["direction"] == "inbound" and entry["provenance"] == "manual"


def test_caller_context_for_phone_app(env):
    app, ids, _ = env
    _identity(ids["a"], "Main", [ids["n1"]])
    c = _as(app, ids["viewer"])
    body = c.get("/api/comms/caller-context", query_string={"from": CUSTOMER, "to": LINE_1}).get_json()
    assert body["contact"]["id"] == ids["contact"] and body["identity"]["name"] == "Main"
    assert "summary" in body["briefing"] and "consent" in body["briefing"]
    unknown = c.get("/api/comms/caller-context", query_string={"from": "+19168670000"}).get_json()
    assert unknown["contact"] is None and unknown["briefing"] is None
    leaked = _as(app, ids["other"]).get("/api/comms/caller-context", query_string={"from": CUSTOMER}).get_json()
    assert leaked["contact"] is None
    assert c.get("/api/comms/caller-context").status_code == 400
