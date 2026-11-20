"""End-to-end SaaS customer journey through the real routes (Phase 3 acceptance).

One test walks a brand-new customer from the marketing site to signing out
and back in. External providers are replaced at their outermost edge only:
outgoing account email is captured, the Stripe SDK is a recording fake (no
network), Stripe webhooks are signed with a test secret and verified by the
real handler, Twilio webhooks are signed with the tenant's token and verified
by the real handler, and the Twilio REST client records instead of sending.
"""
import hashlib
import hmac
import json
import re
import time
from types import SimpleNamespace

import pytest
from flask import g
from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash

import auth as auth_module
import twilio_sms
from app import create_app
from extensions import db
from services.license_service import PHONE_PWA_FEATURE
from models import (
    Company, Contact, ContactTask, TeamInvitation, TwilioAccount, TwilioMessage, TwilioPhoneNumber, User,
    TenantLicense, UserCompanyAccess,
)
from services import entitlements, stripe_billing

WEBHOOK_SECRET = "whsec_journey_test"
TENANT_LINE = "+19165550777"
CUSTOMER_PHONE = "+19165550888"


class _Client(FlaskClient):
    """Each request starts with a clean ``g``, as it does in production.

    The fixture holds one app context, so per-request ``g`` values (the
    signed-in user, the inbound-SMS outbox marker) would otherwise leak
    into the next request.
    """

    def open(self, *args, **kwargs):
        for key in list(vars(g)):
            g.pop(key, None)
        return super().open(*args, **kwargs)


class FakeStripe:
    def __init__(self):
        self.calls = []
        fake = self

        class _Price:
            @staticmethod
            def list(**kw):
                fake.calls.append(("Price.list", kw))
                return SimpleNamespace(data=[SimpleNamespace(id=f"price_{kw['lookup_keys'][0]}")])

        class _Session:
            @staticmethod
            def create(**kw):
                fake.calls.append(("checkout.Session.create", kw))
                return SimpleNamespace(id="cs_test_journey", url="https://checkout.stripe.test/cs_test_journey")

        class _Portal:
            @staticmethod
            def create(**kw):
                fake.calls.append(("billing_portal.Session.create", kw))
                return SimpleNamespace(url="https://billing.stripe.test/p/session")

        class _Subscription:
            @staticmethod
            def retrieve(sub_id, **kw):
                return {"id": sub_id, "items": {"data": []}}

        self.Price = _Price
        self.checkout = SimpleNamespace(Session=_Session)
        self.billing_portal = SimpleNamespace(Session=_Portal)
        self.Subscription = _Subscription


class FakeTwilio:
    def __init__(self):
        self.sent = []
        outer = self

        class _Messages:
            @staticmethod
            def create(**kw):
                outer.sent.append(kw)
                return SimpleNamespace(sid=f"SMout{len(outer.sent)}", status="queued")

        self.messages = _Messages


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SELF_SERVE_SIGNUP_ENABLED", "1")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("TWILIO_STRICT_SIGNATURE", "1")
    mail = []
    monkeypatch.setattr(auth_module, "_send_account_email",
                        lambda to, subject, html: mail.append({"to": to, "subject": subject, "html": html}) or True)
    stripe = FakeStripe()
    monkeypatch.setattr(stripe_billing, "get_stripe", lambda: stripe)
    twilio = FakeTwilio()
    monkeypatch.setattr(twilio_sms, "_build_client", lambda ta: twilio)

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost", RATELIMIT_ENABLED=False)
    app.test_client_class = _Client
    with app.app_context():
        db.create_all()
        tag = str(int(time.time() * 1000))[-8:]
        if db.session.get(Company, 1) is None:
            # Company 1 is the platform tenant and always holds the phone license.
            db.session.add(Company(id=1, name=f"Platform {tag}"))
            db.session.flush()
        if TenantLicense.query.filter_by(company_id=1, feature_key=PHONE_PWA_FEATURE).first() is None:
            # As in production: the platform tenant already holds a phone license,
            # so new workspaces are never seeded one by the fresh-install rule.
            db.session.add(TenantLicense(company_id=1, feature_key=PHONE_PWA_FEATURE, status="active"))
        ops = User(username=f"ops-{tag}", email=f"ops-{tag}@luxit.test", is_admin=True,
                   password_hash=generate_password_hash("ops-password-1"))
        db.session.add(ops)
        db.session.commit()
        yield SimpleNamespace(app=app, mail=mail, stripe=stripe, twilio=twilio, tag=tag, ops_id=ops.id)
        db.session.remove()


def _path(url):
    return url.split("localhost", 1)[1]


def _link(mail, kind):
    return _path(re.search(rf'href="(http[^"]+/auth/{kind}/[^"]+)"', mail["html"]).group(1))


def _signed_stripe(client, event):
    payload = json.dumps(event)
    ts = int(time.time())
    sig = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/api/stripe/webhook", data=payload, content_type="application/json",
                       headers={"Stripe-Signature": f"t={ts},v1={sig}"})


def _signed_twilio(client, path, data, token):
    from twilio.request_validator import RequestValidator
    sig = RequestValidator(token).compute_signature(f"https://luxit.app{path}", data)
    return client.post(path, data=data, headers={"X-Twilio-Signature": sig})


def _login(app, username, password):
    c = app.test_client()
    r = c.post("/auth/login", data={"username": username, "password": password})
    return c, r


def test_new_customer_journey(env):
    app, mail, tag = env.app, env.mail, env.tag
    owner_email = f"owner-{tag}@journey.example"
    visitor = app.test_client()

    # 1. Visit the site.
    for page in ("/", "/pricing", "/auth/signup"):
        assert visitor.get(page).status_code == 200, page

    # 2. Create an account.
    r = visitor.post("/auth/signup", data={
        "org_name": f"Journey Plumbing {tag}", "full_name": "Pat Journey", "email": owner_email,
        "password": "journey-pass-1", "confirm_password": "journey-pass-1", "accept_terms": "1"})
    assert r.status_code == 200 and b"Check your email" in r.data
    owner = User.query.filter_by(email=owner_email).one()
    company_id = owner.default_company_id

    # Not usable before verifying.
    c, _ = _login(app, owner_email, "journey-pass-1")
    assert c.get("/crm/today").status_code == 302

    # 3-4. Verify by email; the link signs the owner in and sends them to billing.
    assert len(mail) == 1 and mail[0]["to"] == owner_email
    owner_c = app.test_client()
    r = owner_c.get(_link(mail[0], "verify-email"))
    assert r.status_code == 302 and "billing" in r.headers["Location"]
    assert owner_c.get("/crm/today").status_code == 200

    # 5. The organization exists, owned by the new user, on the free plan.
    access = UserCompanyAccess.query.filter_by(user_id=owner.id, company_id=company_id).one()
    assert access.role == "owner" and owner.is_admin is False
    assert entitlements.is_free_plan(company_id)
    ent = owner_c.get("/api/account/entitlements").get_json()
    assert ent["plan"]["free"] is True and ent["seats"]["limit"] == 1 and ent["features"]["ai"] is False
    assert ent["contacts"]["limit"] == entitlements.FREE_PLAN_MAX_CONTACTS and ent["can_manage_billing"] is True

    # 6. Onboarding + business settings.
    progress = owner_c.get("/api/onboarding/progress").get_json()["progress"]
    assert progress["checklist"]["company_created"] is True
    r = owner_c.post(f"/api/company/{company_id}/settings", json={"primary_color": "#123456"})
    assert r.status_code == 200 and db.session.get(Company, company_id).primary_color == "#123456"

    # Free plan: one seat, no AI.
    r = owner_c.post("/user/invite", data={"email": f"early-{tag}@journey.example", "role": "editor"})
    assert TeamInvitation.query.filter_by(company_id=company_id).count() == 0
    with pytest.raises(entitlements.PlanLimitReached):
        entitlements.require_ai_entitlement(company_id)

    # 7-8. Choose a plan and complete (sandbox) checkout.
    r = owner_c.post("/api/stripe/create-checkout-session", json={"lookup_key": "luxit_starter_monthly"})
    assert r.status_code == 200 and r.get_json()["url"].startswith("https://checkout.stripe.test/")
    created = [kw for name, kw in env.stripe.calls if name == "checkout.Session.create"][0]
    assert created["client_reference_id"] == str(company_id)
    assert created["line_items"] == [{"price": "price_luxit_starter_monthly", "quantity": 1}]
    assert owner_c.post("/api/stripe/create-checkout-session",
                        json={"lookup_key": "luxit_starter_monthly", "price_id": "price_free"}).status_code == 400

    event = {"id": f"evt_journey_{tag}", "type": "checkout.session.completed", "livemode": False,
             "data": {"object": {"id": "cs_test_journey", "object": "checkout.session", "customer": f"cus_{tag}",
                                 "subscription": f"sub_{tag}", "client_reference_id": str(company_id),
                                 "metadata": {"company_id": str(company_id), "lookup_key": "luxit_starter_monthly",
                                              "include_setup_fee": "false"}}}}
    forged = app.test_client().post("/api/stripe/webhook", data=json.dumps(event), content_type="application/json",
                                    headers={"Stripe-Signature": "t=1,v1=deadbeef"})
    assert forged.status_code == 400
    assert db.session.get(Company, company_id).billing_status != "active"
    assert _signed_stripe(app.test_client(), event).status_code == 200
    replay = _signed_stripe(app.test_client(), event)
    assert replay.status_code == 200 and replay.get_json().get("duplicate") is True

    # 9. Correct entitlements.
    db.session.expire_all()
    company = db.session.get(Company, company_id)
    assert (company.billing_status, company.billing_tier, company.max_team_members) == ("active", "starter", 5)
    assert company.stripe_customer_id == f"cus_{tag}"
    assert not entitlements.is_free_plan(company_id)
    entitlements.require_ai_entitlement(company_id)  # no longer raises
    ent = owner_c.get("/api/account/entitlements").get_json()
    assert (ent["plan"]["tier"], ent["seats"]["limit"], ent["contacts"]["limit"]) == ("starter", 5, 2500)
    assert ent["features"]["ai"] is True and ent["features"]["phone_sms"]["allowed"] is False

    # 10. Invite a team member, who sets their own password.
    mate_email = f"mate-{tag}@journey.example"
    mail.clear()
    assert owner_c.post("/user/invite", data={"email": mate_email, "role": "editor"}).status_code == 302
    token = re.search(r'/auth/invite/([A-Za-z0-9_\-]+)"', mail[0]["html"]).group(1)
    r = app.test_client().post(f"/auth/invite/{token}", data={
        "full_name": "Sam Mate", "password": "mate-pass-12", "confirm_password": "mate-pass-12"})
    assert r.status_code == 302
    mate = User.query.filter_by(email=mate_email).one()
    assert UserCompanyAccess.query.filter_by(user_id=mate.id, company_id=company_id).one().role == "editor"
    mate_c, _ = _login(app, mate_email, "mate-pass-12")
    assert mate_c.get("/crm/today").status_code == 200
    ent = mate_c.get("/api/account/entitlements").get_json()
    assert ent["role"] == "editor" and ent["can_manage_billing"] is False and ent["seats"]["members"] == 2

    # 12. Create contacts.
    r = owner_c.post("/contacts/add", data={"first_name": "Casey", "last_name": "Customer", "phone": CUSTOMER_PHONE,
                                           "email": f"casey-{tag}@journey.example"})
    assert r.status_code in (200, 302)
    casey = Contact.query.filter_by(company_id=company_id, normalized_phone=CUSTOMER_PHONE).one()

    # 13. Connect the tenant's own Twilio account (bring-your-own, D-6). The
    # connect form validates against Twilio's API, so the account row is
    # seeded here; everything after this uses the real routes.
    acct = TwilioAccount(company_id=company_id, from_phone=TENANT_LINE, is_active=True)
    acct.set_account_sid(f"ACjourney{tag}")
    acct.set_auth_token("journey-token")
    db.session.add(acct)
    db.session.flush()
    db.session.add(TwilioPhoneNumber(company_id=company_id, twilio_account_id=acct.id, phone_number=TENANT_LINE,
                                     sms_enabled=True, is_active=True, is_primary=True))
    db.session.commit()

    # 14. Receive a text (signed webhook) …
    inbound = {"From": CUSTOMER_PHONE, "To": TENANT_LINE, "Body": "Can you quote a water heater swap?",
               "MessageSid": f"SMin{tag}"}
    assert app.test_client().post("/twilio/sms/inbound", data=inbound).status_code == 403  # unsigned
    assert _signed_twilio(app.test_client(), "/twilio/sms/inbound", inbound, "journey-token").status_code == 200
    msg = TwilioMessage.query.filter_by(twilio_sid=f"SMin{tag}").one()
    assert msg.company_id == company_id

    # … and reply. A subscription alone doesn't license messaging (T-16): the
    # platform grants the phone/SMS module once the tenant's line is approved.
    r = owner_c.post(f"/api/contacts/{casey.id}/send", json={"channel": "sms", "body": "Yes — sending a quote today."})
    assert env.twilio.sent == [] and not (r.get_json() or {}).get("success")
    ops_c, _ = _login(app, f"ops-{tag}", "ops-password-1")
    assert ops_c.post("/api/global/licenses/grant", json={"company_id": company_id}).status_code == 200
    assert owner_c.get("/api/account/entitlements").get_json()["features"]["phone_sms"]["allowed"] is True
    r = owner_c.post(f"/api/contacts/{casey.id}/send", json={"channel": "sms", "body": "Yes — sending a quote today."})
    assert r.status_code == 200 and r.get_json()["success"] is True, r.get_json()
    assert env.twilio.sent[-1]["from_"] == TENANT_LINE and env.twilio.sent[-1]["to"] == CUSTOMER_PHONE

    # 15. Create and run an automation: every inbound text creates a follow-up task.
    rule = {"name": "Follow up on texts", "triggers": ["inbound_sms.received"], "conditions": {},
            "actions": [{"type": "task.create", "title": "Follow up with {first_name}"}], "is_active": True}
    assert owner_c.post("/api/automations", json=rule).status_code == 201
    inbound2 = {**inbound, "Body": "Also the garbage disposal", "MessageSid": f"SMin2{tag}"}
    assert _signed_twilio(app.test_client(), "/twilio/sms/inbound", inbound2, "journey-token").status_code == 200
    assert ContactTask.query.filter_by(company_id=company_id, title="Follow up with Casey").count() == 1

    # 16. CRM + Ask LUXit.
    r = mate_c.post("/api/crm/opportunities", json={"contact_id": casey.id, "name": "Water heater", "estimated_value": "2400"})
    assert r.status_code == 201
    ask = mate_c.post("/api/ask", json={"question": "Which texts are waiting for a reply?"}).get_json()
    assert ask["success"] and ask["results"] and ask["results"][0]["contact_id"] == casey.id
    ask = mate_c.post("/api/ask", json={"question": "Show open deals"}).get_json()
    assert "$2,400" in ask["answer"]

    # 17. Manage billing: owner reaches the portal, a teammate can't.
    assert mate_c.post("/api/stripe/create-portal-session", json={}).status_code == 403
    r = owner_c.post("/api/stripe/create-portal-session", json={})
    assert r.status_code == 200 and "billing.stripe.test" in json.dumps(r.get_json())

    # 18. Sign out and come back.
    owner_c.get("/auth/logout")
    assert owner_c.get("/crm/today").status_code == 302
    back, r = _login(app, owner_email, "journey-pass-1")
    assert back.get("/crm/today").status_code == 200

    # Isolation spot-check: the ops user's own (empty) workspace and other tenants see nothing of this one.
    other_owner = User(username=f"stranger-{tag}", email=f"stranger-{tag}@journey.example",
                       password_hash=generate_password_hash("stranger-pass-1"))
    db.session.add(other_owner)
    db.session.flush()
    other_co = Company(name=f"Stranger {tag}")
    db.session.add(other_co)
    db.session.flush()
    other_owner.default_company_id = other_co.id
    db.session.add(UserCompanyAccess(user_id=other_owner.id, company_id=other_co.id, role="owner", is_default=True))
    db.session.commit()
    stranger, _ = _login(app, f"stranger-{tag}", "stranger-pass-1")
    assert stranger.post("/api/ask", json={"question": "water heater"}).get_json()["results"] == []
    assert stranger.get(f"/api/contacts/{casey.id}/timeline").status_code in (403, 404)

    # 19. Manage the account: a password change signs out the owner's other devices.
    phone_c, _ = _login(app, owner_email, "journey-pass-1")
    assert phone_c.get("/crm/today").status_code == 200
    r = back.post("/user/change-password", data={"current_password": "journey-pass-1",
                                                 "new_password": "journey-pass-2", "confirm_password": "journey-pass-2"})
    assert r.status_code == 302
    assert phone_c.get("/crm/today").status_code == 302
    assert back.get("/crm/today").status_code == 200
    assert _login(app, owner_email, "journey-pass-1")[0].get("/crm/today").status_code == 302
    back, _ = _login(app, owner_email, "journey-pass-2")

    # 20. Card declined → grace keeps the plan; subscription ends → free limits, data kept.
    declined = {"id": f"evt_declined_{tag}", "type": "invoice.payment_failed", "livemode": False,
                "data": {"object": {"id": f"in_{tag}", "object": "invoice", "customer": f"cus_{tag}",
                                    "subscription": f"sub_{tag}", "attempt_count": 1}}}
    assert _signed_stripe(app.test_client(), declined).status_code == 200
    db.session.expire_all()
    assert db.session.get(Company, company_id).billing_status == "grace_period"
    assert not entitlements.is_free_plan(company_id)
    ended = {"id": f"evt_ended_{tag}", "type": "customer.subscription.deleted", "livemode": False,
             "data": {"object": {"id": f"sub_{tag}", "object": "subscription", "customer": f"cus_{tag}",
                                 "status": "canceled"}}}
    assert _signed_stripe(app.test_client(), ended).status_code == 200
    db.session.expire_all()
    assert db.session.get(Company, company_id).billing_status == "suspended"
    assert entitlements.is_free_plan(company_id)
    assert stranger.get(f"/api/contacts/{casey.id}/timeline").status_code in (403, 404)
    assert back.get(f"/api/contacts/{casey.id}/timeline").status_code == 200
