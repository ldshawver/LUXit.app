from datetime import datetime, timedelta
import json

import pytest
from flask_login import FlaskLoginClient
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import (
    BillingAutomationRule, Company, FeatureModule, LicenseEventLog, SMSCampaign,
    TenantBillingAccount, TenantInvoice, TenantLicense, TwilioAccount,
    TwilioConversation, TwilioPhoneNumber, User, UserCompanyAccess,
)
from services.license_service import (
    PHONE_PWA_FEATURE, auto_suspend_past_due, get_company_license,
    has_feature, reactivate_license, seed_feature_modules, sync_license_from_stripe_event,
)
from services.sms_service import SMSService


@pytest.fixture
def license_ctx(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="license-test", SERVER_NAME="localhost")
    app.test_client_class = FlaskLoginClient
    with app.app_context():
        db.create_all()
        co1 = Company(name="MyOrder Fun", stripe_customer_id="cus_myorder")
        co2 = Company(name="Other Tenant", stripe_customer_id="cus_other_tenant")
        db.session.add_all([co1, co2]); db.session.flush()
        admin = User(username="tenant-admin", email="tenant-admin@example.com", password_hash=generate_password_hash("pw"), default_company_id=co1.id)
        regular = User(username="regular-user", email="regular-user@example.com", password_hash=generate_password_hash("pw"), default_company_id=co1.id)
        global_admin = User(username="global-admin", email="global-admin@example.com", password_hash=generate_password_hash("pw"), is_admin=True, default_company_id=co1.id)
        db.session.add_all([admin, regular, global_admin]); db.session.flush()
        db.session.add_all([
            UserCompanyAccess(user_id=admin.id, company_id=co1.id, role="admin", can_access_mobile_inbox=True, pwa_access_enabled=True),
            UserCompanyAccess(user_id=regular.id, company_id=co1.id, role="member", can_access_mobile_inbox=True, pwa_access_enabled=True),
            UserCompanyAccess(user_id=global_admin.id, company_id=co1.id, role="owner", can_access_mobile_inbox=True, pwa_access_enabled=True),
        ])
        seed_feature_modules(commit=False)
        phone_license = TenantLicense(company_id=co1.id, feature_key=PHONE_PWA_FEATURE, status="active", seats_included=5, seats_used=1, monthly_price=99, renews_at=datetime.utcnow() + timedelta(days=30))
        pos_license = TenantLicense(company_id=co1.id, feature_key="pos_myorder", status="active")
        other_license = TenantLicense(company_id=co2.id, feature_key=PHONE_PWA_FEATURE, status="active")
        billing = TenantBillingAccount(company_id=co1.id, stripe_customer_id="cus_myorder", billing_email="billing@myorder.fun", autopay_enabled=False, payment_status="active")
        inv1 = TenantInvoice(company_id=co1.id, stripe_invoice_id="in_1", invoice_number="INV-1", status="open", amount_due=1000)
        inv2 = TenantInvoice(company_id=co2.id, stripe_invoice_id="in_2", invoice_number="INV-2", status="open", amount_due=2000)
        line = TwilioPhoneNumber(company_id=co1.id, phone_number="+15550001111", sms_enabled=True, voice_enabled=True, is_active=True, is_primary=True)
        ta = TwilioAccount(company_id=co1.id, from_phone="+15550001111", is_active=True, automation_enabled=True)
        ta.set_account_sid("AC123")
        ta.set_auth_token("token")
        db.session.add_all([phone_license, pos_license, other_license, billing, inv1, inv2, line, ta])
        db.session.commit()
        client = app.test_client()
        yield app, client, co1, co2, admin, regular, global_admin, phone_license, line
        db.session.remove(); db.drop_all()


def login(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_tenant_admin_pages_and_billing_scope(license_ctx):
    _, client, co1, co2, admin, *_ = license_ctx
    login(client, admin)
    assert client.get("/settings/licenses").status_code == 200
    assert client.get("/settings/billing").status_code == 200
    invoices = client.get("/api/billing/invoices").get_json()["invoices"]
    assert [i["company_id"] for i in invoices] == [co1.id]
    assert all(i["company_id"] != co2.id for i in invoices)


def test_regular_user_cannot_access_billing(license_ctx):
    app, _, _, _, admin, regular, *_ = license_ctx
    assert admin.id != regular.id
    regular_client = app.test_client()
    login(regular_client, regular)
    assert regular_client.get("/settings/billing").status_code == 403


def test_global_admin_can_manage_all_licenses_and_event_log_records(license_ctx):
    app, client, co1, _, _, _, global_admin, phone_license, _ = license_ctx
    login(client, global_admin)
    assert client.get("/global-admin/licenses").status_code == 200
    suspend = client.post(f"/api/global/licenses/{phone_license.id}/suspend", json={"reason": "non_payment"})
    assert suspend.status_code == 200
    assert suspend.get_json()["license"]["status"] == "suspended"
    reactivate = client.post(f"/api/global/licenses/{phone_license.id}/reactivate")
    assert reactivate.status_code == 200
    assert reactivate.get_json()["license"]["status"] == "active"
    with app.app_context():
        events = LicenseEventLog.query.filter_by(company_id=co1.id).all()
        assert any(e.event_type == "license_suspended" for e in events)
        assert any(e.event_type == "license_reactivated" for e in events)


def test_suspended_phone_license_blocks_pwa_and_outbound_but_not_inbound_logging(license_ctx, monkeypatch):
    app, client, co1, _, admin, _, global_admin, phone_license, line = license_ctx
    reactivate_license(co1.id, PHONE_PWA_FEATURE)
    login(client, global_admin)
    client.post(f"/api/global/licenses/{phone_license.id}/suspend", json={"reason": "non_payment"})
    login(client, admin)
    assert client.get("/app/inbox", headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile"}).status_code == 402
    assert client.get("/app/calls", headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile"}).status_code == 402
    assert SMSService.send_sms("+15551110000", "hello", company_id=co1.id)["status_code"] == 402
    camp = SMSCampaign(company_id=co1.id, name="Blocked", message="Hi", status="draft")
    db.session.add(camp); db.session.commit()
    assert SMSService.begin_send(camp.id)[1]["status_code"] == 402
    resp = client.post("/twilio/sms/inbound", data={"From": "+15554443333", "To": line.phone_number, "Body": "hello", "MessageSid": "SMLICENSEBLOCK"})
    assert resp.status_code == 200
    with app.app_context():
        assert TwilioConversation.query.filter_by(company_id=co1.id, from_number="+15554443333").first() is not None


def test_past_due_grace_allows_then_auto_suspends_and_reactivation_restores_access(license_ctx):
    app, client, co1, _, admin, *_ = license_ctx
    lic = get_company_license(co1.id, PHONE_PWA_FEATURE)
    lic.status = "past_due"
    lic.renews_at = datetime.utcnow()
    lic.grace_period_days = 7
    db.session.commit()
    login(client, admin)
    assert client.get("/app/inbox", headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile"}).status_code == 200
    lic.renews_at = datetime.utcnow() - timedelta(days=10)
    db.session.commit()
    changed = auto_suspend_past_due(now=datetime.utcnow())
    assert lic.id in changed
    assert client.get("/app/inbox", headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile"}).status_code == 402
    reactivate_license(co1.id, PHONE_PWA_FEATURE)
    assert client.get("/app/inbox", headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile"}).status_code == 200


def test_stripe_events_sync_billing_license_invoice_and_automation(license_ctx):
    app, client, co1, *_ = license_ctx
    rule = BillingAutomationRule(scope="global", event_type="payment_failed", action="email_admin", enabled=True)
    db.session.add(rule); db.session.commit()
    failed_event = {"id": "evt_fail", "type": "invoice.payment_failed", "data": {"object": {"id": "in_fail", "customer": "cus_myorder", "subscription": "sub_1", "status": "open", "amount_due": 1234, "attempt_count": 1}}}
    assert sync_license_from_stripe_event(failed_event)["success"] is True
    lic = get_company_license(co1.id, PHONE_PWA_FEATURE)
    assert lic.status == "past_due"
    assert TenantBillingAccount.query.filter_by(company_id=co1.id).first().payment_status == "past_due"
    assert TenantInvoice.query.filter_by(stripe_invoice_id="in_fail").first().amount_due == 1234
    assert LicenseEventLog.query.filter(LicenseEventLog.event_type.like("billing_automation:%")).first() is not None
    success_event = {"id": "evt_paid", "type": "invoice.payment_succeeded", "data": {"object": {"id": "in_fail", "customer": "cus_myorder", "subscription": "sub_1", "status": "paid", "amount_due": 1234, "amount_paid": 1234}}}
    assert sync_license_from_stripe_event(success_event)["success"] is True
    assert get_company_license(co1.id, PHONE_PWA_FEATURE).status == "active"


def test_stripe_webhook_rejects_invalid_signature(license_ctx):
    _, client, *_ = license_ctx
    resp = client.post("/api/stripe/webhook", data=json.dumps({"id": "evt_bad", "type": "invoice.payment_failed", "data": {"object": {}}}), headers={"Stripe-Signature": "bad"})
    assert resp.status_code == 400


def test_autopay_persists_pos_license_is_independent_and_existing_company_access(license_ctx):
    app, client, co1, _, admin, *_ = license_ctx
    login(client, admin)
    resp = client.post("/api/billing/autopay", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.get_json()["autopay_enabled"] is True
    assert has_feature(co1.id, PHONE_PWA_FEATURE) is True
    assert get_company_license(co1.id, "pos_myorder").status == "active"
    assert client.get("/app/inbox", headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile"}).status_code == 200


def test_migration_file_is_idempotent_and_seeds_modules():
    sql = open("migrations/20260621_license_billing_feature_management.sql", encoding="utf-8").read()
    for phrase in ["CREATE TABLE IF NOT EXISTS", "ADD COLUMN IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS", "ON CONFLICT", "phone_pwa_communications", "pos_myorder"]:
        assert phrase in sql


def test_live_acceptance_script_covers_required_vps_proof_steps():
    script = open("scripts/verify_license_live_acceptance.sh", encoding="utf-8").read()
    for phrase in [
        "migrations/20260621_license_billing_feature_management.sql",
        "SELECT key, is_active FROM feature_module",
        "company_id=1 AND feature_key='phone_pwa_communications' AND status='active'",
        "/settings/licenses",
        "/settings/billing",
        "/global-admin/licenses",
        "suspend_license(1, 'phone_pwa_communications'",
        "inbox_status_while_suspended",
        "/twilio/sms/inbound",
        "invoice.payment_failed",
        "invoice.payment_succeeded",
        "reactivate_license(1, 'phone_pwa_communications'",
        "license_event_log",
        "UndefinedColumn|BuildError|ProgrammingError|InFailedSqlTransaction",
        "LIVE LICENSE ACCEPTANCE: PASS",
    ]:
        assert phrase in script
