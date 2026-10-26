"""T-16: only platform workspaces may use the platform Twilio account.

A tenant without its own Twilio sender must be refused rather than silently
sending SMS from LUXit's number or minting a Voice token on LUXit's account,
and buying a plan must not hand out the phone/PWA module.
"""

from datetime import datetime, timedelta

import pytest
from flask_login import FlaskLoginClient
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import Company, TenantLicense, TwilioAccount, User, UserCompanyAccess
from services.license_service import (
    PHONE_PWA_FEATURE, get_company_license, sync_license_from_stripe_event, uses_platform_sender,
)
from services.sms_service import SMSService


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.delenv("LUXIT_PLATFORM_SENDER_COMPANY_IDS", raising=False)
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="sender-test", SERVER_NAME="localhost")
    app.test_client_class = FlaskLoginClient
    with app.app_context():
        db.create_all()
        platform = Company(name="Platform Co", stripe_customer_id="cus_platform")
        tenant = Company(name="Tenant Co", stripe_customer_id="cus_tenant")
        unlicensed = Company(name="New Signup Co", stripe_customer_id="cus_new")
        db.session.add_all([platform, tenant, unlicensed]); db.session.flush()
        assert platform.id == 1
        renews = datetime.utcnow() + timedelta(days=30)
        db.session.add_all([
            TenantLicense(company_id=platform.id, feature_key=PHONE_PWA_FEATURE, status="active", renews_at=renews),
            TenantLicense(company_id=tenant.id, feature_key=PHONE_PWA_FEATURE, status="active", renews_at=renews),
        ])
        db.session.commit()
        yield app, platform, tenant, unlicensed
        db.session.remove(); db.drop_all()


class _FakeMessages:
    def __init__(self):
        self.sent = []

    def create(self, **kwargs):
        self.sent.append(kwargs)
        return type("Msg", (), {"sid": "SMfake", "status": "queued"})()


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


@pytest.fixture
def platform_client(monkeypatch):
    client = _FakeClient()

    def _init():
        SMSService._twilio_client = client
        SMSService._twilio_phone = "+15550009999"
        return True

    monkeypatch.setattr(SMSService, "_init_twilio", classmethod(lambda cls: _init()))
    return client


def test_send_without_workspace_is_refused(ctx, platform_client):
    result = SMSService.send_sms("+15551230000", "hi", company_id=None)
    assert result["success"] is False and result["status_code"] == 403
    assert platform_client.messages.sent == []


def test_licensed_tenant_without_own_sender_cannot_use_platform_number(ctx, platform_client):
    _, _, tenant, _ = ctx
    result = SMSService.send_sms("+15551230000", "hi", company_id=tenant.id)
    assert result["success"] is False
    assert result["status_code"] == 409 and result["sender_missing"] is True
    assert platform_client.messages.sent == []


def test_platform_workspace_keeps_platform_fallback(ctx, platform_client):
    _, platform, _, _ = ctx
    result = SMSService.send_sms("+15551230000", "hi", company_id=platform.id)
    assert result["success"] is True
    assert platform_client.messages.sent[0]["from_"] == "+15550009999"


def test_tenant_with_own_sender_uses_its_own_account(ctx, platform_client, monkeypatch):
    _, _, tenant, _ = ctx
    own = _FakeClient()
    monkeypatch.setattr(SMSService, "_tenant_twilio_config", staticmethod(
        lambda cid: {"client": own, "messaging_service_sid": None, "from_phone": "+15550004444"} if cid == tenant.id else None
    ))
    result = SMSService.send_sms("+15551230000", "hi", company_id=tenant.id)
    assert result["success"] is True
    assert own.messages.sent[0]["from_"] == "+15550004444"
    assert platform_client.messages.sent == []


def test_platform_sender_allowlist_is_configurable(ctx, monkeypatch):
    _, platform, tenant, _ = ctx
    assert uses_platform_sender(platform.id) and not uses_platform_sender(tenant.id)
    monkeypatch.setenv("LUXIT_PLATFORM_SENDER_COMPANY_IDS", f"{platform.id}, {tenant.id}")
    assert uses_platform_sender(tenant.id)
    assert not uses_platform_sender(None)


def test_plan_subscription_does_not_grant_phone_module(ctx):
    _, _, _, unlicensed = ctx
    event = {"id": "evt_sub", "type": "customer.subscription.created", "data": {"object": {
        "id": "sub_new", "customer": "cus_new", "status": "active",
        "current_period_end": int((datetime.utcnow() + timedelta(days=30)).timestamp()),
    }}}
    result = sync_license_from_stripe_event(event)
    assert result["success"] is True and result["status"] == "missing"
    assert TenantLicense.query.filter_by(company_id=unlicensed.id, feature_key=PHONE_PWA_FEATURE).count() == 0


def test_plan_events_still_update_an_existing_phone_license(ctx):
    _, _, tenant, _ = ctx
    event = {"id": "evt_fail", "type": "invoice.payment_failed", "data": {"object": {
        "id": "in_x", "customer": "cus_tenant", "subscription": "sub_t", "status": "open", "amount_due": 100,
    }}}
    assert sync_license_from_stripe_event(event)["status"] == "past_due"
    assert get_company_license(tenant.id, PHONE_PWA_FEATURE).status == "past_due"


def test_voice_token_refused_for_non_platform_workspace(ctx, monkeypatch):
    app, _, tenant, _ = ctx
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACplatform")
    monkeypatch.setenv("TWILIO_API_KEY", "SKplatform")
    monkeypatch.setenv("TWILIO_API_SECRET", "secret")
    user = User(username="tenant-owner", email="owner@tenant.test", password_hash=generate_password_hash("pw"), default_company_id=tenant.id)
    db.session.add(user); db.session.flush()
    db.session.add(UserCompanyAccess(user_id=user.id, company_id=tenant.id, role="owner", can_access_mobile_inbox=True, pwa_access_enabled=True))
    ta = TwilioAccount(company_id=tenant.id, from_phone="+15550004444", is_active=True)
    ta.set_account_sid("ACtenant"); ta.set_auth_token("tok")
    db.session.add(ta); db.session.commit()
    monkeypatch.setattr("inbox_pwa.is_available", lambda *a, **k: True, raising=False)
    monkeypatch.setattr("inbox_pwa.accessible_phone_numbers", lambda *a, **k: ["+15550004444"], raising=False)
    with app.test_client(user=user) as client:
        resp = client.get("/api/phone/voice-token")
    body = resp.get_json() or {}
    assert resp.status_code == 403, body
    assert body.get("code") == "CALLING_NOT_PROVISIONED"
    assert "token" not in body
