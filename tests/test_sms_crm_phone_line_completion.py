from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import Company, Contact, SMSCampaign, SMSRecipient, TwilioPhoneNumber, User, UserCompanyAccess, IntegrationAuditLog
from services.phone_line_service import PhoneLineService
from services.sms_service import SMSService


@pytest.fixture
def client_ctx():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        c1 = Company(name="Tenant One")
        c2 = Company(name="Tenant Two")
        db.session.add_all([c1, c2]); db.session.flush()
        user = User(username="admin1", email="admin1@example.com", password_hash=generate_password_hash("pw"), is_admin=True, default_company_id=c1.id)
        db.session.add(user); db.session.flush()
        db.session.add(UserCompanyAccess(user_id=user.id, company_id=c1.id, role="admin", is_default=True))
        line1 = TwilioPhoneNumber(company_id=c1.id, phone_number="+15550001000", friendly_name="Main", sms_enabled=True, voice_enabled=True, is_active=True, is_primary=True)
        line2 = TwilioPhoneNumber(company_id=c2.id, phone_number="+15550002000", friendly_name="Other", sms_enabled=True, voice_enabled=True, is_active=True, is_primary=True)
        db.session.add_all([line1, line2]); db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        yield app, client, c1, c2, user, line1, line2
        db.session.remove(); db.drop_all()


def test_sms_create_and_left_nav_canonical_route_load(client_ctx):
    _, client, *_ = client_ctx
    resp = client.get("/sms/create")
    assert resp.status_code == 200
    assert b"Sender Phone Line" in resp.data
    nav = client.get("/sms/campaigns")
    assert nav.status_code == 200
    assert b"Phone Lines & Webhooks" in nav.data


def test_phone_line_management_ui_saves_per_number_webhooks(client_ctx):
    app, client, c1, _, _, line1, _ = client_ctx
    page = client.get("/settings/phone-lines")
    assert page.status_code == 200
    assert b"Inbound SMS Webhook" in page.data
    resp = client.post(f"/settings/phone-lines/{line1.id}", data={
        "sms_webhook_url": "https://example.test/sms",
        "voice_webhook_url": "https://example.test/voice",
        "status_callback_webhook_url": "https://example.test/status",
        "timezone": "America/New_York",
        "number_auto_reply_text": "Thanks for texting Main",
        "missed_call_text": "Sorry we missed you",
        "voicemail_greeting_text": "Leave a message",
        "after_hours_text": "We are closed",
        "auto_reply_enabled": "on",
        "after_hours_sms_enabled": "on",
        "after_hours_voicemail_enabled": "on",
        "campaign_sender_enabled": "on",
        "campaign_send_rate_per_minute": "12",
    })
    assert resp.status_code in (302, 303)
    with app.app_context():
        saved = db.session.get(TwilioPhoneNumber, line1.id)
        assert saved.sms_webhook_url.endswith("/sms")
        assert saved.status_callback_webhook_url.endswith("/status")
        assert saved.number_auto_reply_text == "Thanks for texting Main"
        assert saved.missed_call_text == "Sorry we missed you"
        assert saved.voicemail_greeting_text == "Leave a message"
        assert saved.after_hours_text == "We are closed"
        assert saved.auto_reply_enabled is True
        assert saved.after_hours_sms_enabled is True
        assert saved.after_hours_voicemail_enabled is True
        assert saved.campaign_send_rate_per_minute == 12
        assert saved.allow_global_fallback is False


def test_per_number_resolution_and_sender_permission_are_tenant_safe(client_ctx):
    app, _, c1, c2, _, line1, line2 = client_ctx
    with app.app_context():
        resolved = PhoneLineService.resolve_by_to_number("+15550001000", purpose="sms")
        assert resolved["company_id"] == c1.id
        assert resolved["phone_number"].id == line1.id
        denied = PhoneLineService.resolve_campaign_sender(c1.id, line2.id)
        assert denied["success"] is False
        allowed = PhoneLineService.resolve_campaign_sender(c1.id, line1.id)
        assert allowed["success"] is True
        assert allowed["source"] == "phone_number_settings"
        assert IntegrationAuditLog.query.filter_by(company_id=c1.id, service_slug="phone_line_resolution").count() >= 2


def test_scheduled_campaign_edit_duplicate_cancel_delete_and_test_send(client_ctx, monkeypatch):
    app, client, c1, _, _, line1, _ = client_ctx
    with app.app_context():
        contact = Contact(company_id=c1.id, first_name="Opt", phone="+15551110000", sms_marketing_opt_in=True, sms_consent_status="opted_in")
        db.session.add(contact); db.session.commit()
    scheduled = (datetime.utcnow() + timedelta(days=1))
    create = client.post("/sms/create", data={
        "name": "Sched", "message": "Visit https://lux.test", "send_option": "scheduled",
        "scheduled_date": scheduled.strftime("%Y-%m-%d"), "scheduled_time": scheduled.strftime("%H:%M"),
        "from_phone_number_id": str(line1.id), "media_urls": "https://example.test/image.jpg", "batch_size": "5", "send_rate_per_minute": "10",
    })
    assert create.status_code in (302, 303)
    with app.app_context():
        campaign = SMSCampaign.query.filter_by(name="Sched", company_id=c1.id).one()
        assert campaign.status == "scheduled"
    edited_time = scheduled + timedelta(days=1)
    edit = client.post(f"/sms/campaign/{campaign.id}/edit", data={
        "name": "Sched Edited", "message": "New body", "scheduled_date": edited_time.strftime("%Y-%m-%d"),
        "scheduled_time": edited_time.strftime("%H:%M"), "from_phone_number_id": str(line1.id), "batch_size": "3", "send_rate_per_minute": "9",
    })
    assert edit.status_code in (302, 303)
    dup = client.post(f"/sms/campaign/{campaign.id}/duplicate")
    assert dup.status_code in (302, 303)
    sent = []
    monkeypatch.setattr(SMSService, "send_sms", staticmethod(lambda *a, **kw: sent.append((a, kw)) or {"success": True, "message_sid": "SMTEST"}))
    test = client.post(f"/sms/campaign/{campaign.id}/test-send", data={"test_number": "+15551112222"})
    assert test.status_code == 200
    cancel = client.post(f"/sms/campaign/{campaign.id}/cancel")
    assert cancel.status_code in (302, 303)
    with app.app_context():
        assert SMSCampaign.query.filter(SMSCampaign.name.like("%Copy")).count() == 1
        assert db.session.get(SMSCampaign, campaign.id).status == "canceled"
    delete = client.post(f"/sms/campaign/{campaign.id}/delete")
    assert delete.status_code in (302, 303)


def test_contact_import_preview_maps_opt_in_out_and_duplicates(client_ctx):
    app, client, c1, *_ = client_ctx
    with app.app_context():
        db.session.add(Contact(company_id=c1.id, email="dupe@example.com", phone="+15553334444")); db.session.commit()
    csv_body = "first_name,last_name,phone,email,sms_marketing_opt_in,do_not_market,sms_opted_out,email_unsubscribed,tags,notes\nJane,Doe,+15553334444,dupe@example.com,yes,no,true,true,vip,hello\n"
    resp = client.post("/contacts/import/preview", data={"file": (BytesIO(csv_body.encode()), "contacts.csv")}, content_type="multipart/form-data")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["duplicates"] == 1
    row = data["preview"][0]
    assert row["sms_marketing_opt_in"] is True
    assert row["sms_opted_out"] is True
    assert row["email_unsubscribed"] is True


def test_batch_send_rate_limit_prevents_duplicate_sends_and_resumes(client_ctx, monkeypatch):
    app, _, c1, *_ = client_ctx
    with app.app_context():
        campaign = SMSCampaign(company_id=c1.id, name="Batch", message="Hi Reply STOP to opt out.", status="draft", batch_size=2, send_rate_per_minute=2)
        db.session.add(campaign); db.session.flush()
        for n in range(3):
            db.session.add(SMSRecipient(company_id=c1.id, campaign_id=campaign.id, phone_number=f"+1555000000{n}", status="pending"))
        db.session.commit()
        monkeypatch.setattr(SMSService, "send_sms", staticmethod(lambda *a, **kw: {"success": True, "message_sid": f"SM{a[0][-1]}"}))
        first = SMSService.send_campaign(campaign.id)
        assert first["sent"] == 2
        assert db.session.get(SMSCampaign, campaign.id).status == "scheduled"
        second = SMSService.send_campaign(campaign.id)
        assert second["success"] is False
        assert second["status_code"] == 409
        # Scheduler resume path sends remaining pending recipients without duplicating sent rows.
        resumed = SMSService.send_campaign(campaign.id, transition=False)
        assert resumed["sent"] == 1
        assert SMSRecipient.query.filter_by(campaign_id=campaign.id, status="sent").count() == 3


def test_calendar_analytics_and_ai_include_sms_campaigns(client_ctx):
    app, client, c1, *_ = client_ctx
    when = datetime.utcnow() + timedelta(days=2)
    with app.app_context():
        campaign = SMSCampaign(company_id=c1.id, name="Calendar SMS", message="Hi", status="scheduled", scheduled_at=when)
        db.session.add(campaign); db.session.flush()
        db.session.add(SMSRecipient(company_id=c1.id, campaign_id=campaign.id, phone_number="+15554440000", status="sent"))
        db.session.commit()
    events = client.get("/api/calendar/events")
    assert events.status_code == 200
    assert "Calendar SMS" in events.get_data(as_text=True)
    ai = client.get("/api/ai/sms-campaign-context")
    assert ai.status_code == 200
    data = ai.get_json()["context"]
    assert any(c["name"] == "Calendar SMS" for c in data["scheduled"])
    assert data["scheduled"][0]["metrics"]["sent"] == 1


def test_final_route_health_legacy_redirects_and_migration_idempotency(client_ctx):
    _, client, *_ = client_ctx
    checks = [
        ("/sms/create", 200),
        ("/app/sms-campaigns", 200),
        ("/settings/phone-lines", 200),
        ("/admin/phone-lines", 200),
        ("/admin/communications", 200),
    ]
    for path, expected in checks:
        resp = client.get(path)
        assert resp.status_code == expected, (path, resp.status_code, resp.get_data(as_text=True)[:300])
    legacy = client.get("/sms-dashboard", follow_redirects=False)
    assert legacy.status_code in (301, 302, 303, 308)
    assert legacy.headers["Location"].endswith(("/sms/campaigns", "/app/sms-campaigns"))
    page = client.get("/sms/create")
    assert ('href="/sms/campaigns"' in page.get_data(as_text=True) or 'href="/app/sms-campaigns"' in page.get_data(as_text=True))

    sql = open("migrations/20260619_sms_phone_line_campaign_completion.sql", encoding="utf-8").read().lower()
    assert "add column if not exists" in sql
    assert "create index if not exists" in sql
    assert " drop " not in sql
    assert "delete from" not in sql


def test_non_admin_sender_permissions_require_assigned_line(client_ctx):
    app, _, c1, _, _, line1, _ = client_ctx
    with app.app_context():
        from models import PhoneNumberUserPermission
        staff = User(username="staff", email="staff@example.com", password_hash=generate_password_hash("pw"), is_admin=False, default_company_id=c1.id)
        db.session.add(staff); db.session.flush()
        db.session.add(UserCompanyAccess(user_id=staff.id, company_id=c1.id, role="viewer", is_default=True))
        denied = PhoneLineService.resolve_campaign_sender(c1.id, line1.id, user=staff)
        assert denied["success"] is False
        db.session.add(PhoneNumberUserPermission(company_id=c1.id, user_id=staff.id, phone_number_id=line1.id, can_access_pwa=True))
        db.session.commit()
        allowed = PhoneLineService.resolve_campaign_sender(c1.id, line1.id, user=staff)
        assert allowed["success"] is True


def test_live_gate_workflow_and_comms_pwa_routes(client_ctx):
    app, client, c1, _, user, line1, _ = client_ctx
    # Production deploy workflow must run migrations/*.sql through psql.
    workflow = open(".github/workflows/push-to-production.yml", encoding="utf-8").read()
    assert "migrations/*.sql" in workflow
    assert 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"' in workflow

    # Admin route health for live-gate routes requested by review.
    for path in ("/twilio/comms", "/app/inbox"):
        resp = client.get(path, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile"})
        assert resp.status_code == 200, (path, resp.status_code, resp.get_data(as_text=True)[:300])

    # Staff/mobile-inbox-only user can access PWA inbox and shared permitted-number history
    # without manage/campaign permissions. Read conversations remain in filter=all.
    with app.app_context():
        from models import PhoneNumberUserPermission, TwilioConversation
        staff = User(username="pwa-staff", email="pwa-staff@example.com", password_hash=generate_password_hash("pw"), is_admin=False, default_company_id=c1.id)
        db.session.add(staff); db.session.flush()
        db.session.add(UserCompanyAccess(
            user_id=staff.id, company_id=c1.id, role="inbox_only", is_default=True,
            can_access_mobile_inbox=True, pwa_access_enabled=True, can_access_full_app=False,
            manage_users_enabled=False, comms_hub_enabled=False, communications_license=False,
        ))
        db.session.add(PhoneNumberUserPermission(company_id=c1.id, user_id=staff.id, phone_number_id=line1.id, can_access_pwa=True))
        conv = TwilioConversation(
            company_id=c1.id, phone_number_id=line1.id, from_number="+15554443333", to_number=line1.phone_number,
            contact_name="Shared Caller", is_read=True, last_message_preview="Read but visible", last_message_at=datetime.utcnow(),
        )
        db.session.add(conv); db.session.commit()
        staff_id = staff.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(staff_id)
        sess["_fresh"] = True
    inbox = client.get("/app/inbox", headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile"})
    assert inbox.status_code == 200
    data_resp = client.get("/api/inbox/conversations?filter=all")
    assert data_resp.status_code == 200
    data = data_resp.get_json()
    assert data["success"] is True
    assert any(c["from_number"] == "+15554443333" for c in data["conversations"])



def test_codex_instructions_require_audit_find_fix_retest_report_workflow():
    doc = open("docs/SMS_CRM_CAMPAIGN_CODEX_INSTRUCTIONS.md", encoding="utf-8").read()
    required_phrases = [
        "Required audit action workflow before PR completion",
        "### 1. AUDIT",
        "### 2. FINDINGS",
        "### 3. FIX",
        "### 4. RETEST",
        "### 5. VERIFY",
        "### 6. REPORT",
        "Do not accept a documentation-only or partial audit response",
        "/api/inbox/conversations?filter=all",
        "UndefinedColumn",
        "Company\\.query\\.first",
    ]
    for phrase in required_phrases:
        assert phrase in doc


def test_pwa_deployment_verification_has_executable_vps_steps():
    doc = open("docs/SMS_PHONE_LINE_DEPLOYMENT_VERIFICATION.md", encoding="utf-8").read()
    required_phrases = [
        "psql \"$DATABASE_URL\" -v ON_ERROR_STOP=1 -f migrations/20260621_pwa_alerts_greetings.sql",
        "python scripts/verify_pwa_live_acceptance.py --write-tests --run-live-reminder",
        "SUMMARY failed=0",
        "VAPID_PUBLIC_KEY",
        "VAPID_PRIVATE_KEY",
        "VAPID_SUBJECT",
        "luxit-pwa-unread-reminders.timer",
        "scripts/run_pwa_unread_reminders.py",
        "python scripts/run_pwa_unread_reminders.py --dry-run",
        "sudo systemctl restart lux-email-bot",
        "$APP_URL/api/pwa/push/status",
        "$APP_URL/api/pwa/preferences",
        "$APP_URL/api/calls/<call_id>/voicemail/audio",
        "$APP_URL/api/phone/numbers/<phone_number_id>/greetings",
        "$APP_URL/api/phone/greetings/${GREETING_ID}/activate",
        "$APP_URL/api/pwa/reminders/unread/run",
        "$APP_URL/api/pwa/reminders/unread/run?dry_run=1",
        "would_create",
        "InFailedSqlTransaction",
        "iOS and some browsers restrict background notification sound",
        "`/twilio/voice/inbound` 500 handling is fixed by this PR",
    ]
    for phrase in required_phrases:
        assert phrase in doc
