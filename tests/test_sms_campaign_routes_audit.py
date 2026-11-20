from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import Company, Contact, Segment, SMSCampaign, SMSTemplate, TwilioAccount, TwilioPhoneNumber, User, UserCompanyAccess


def _login_fixture():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        company = Company(name="SMS Route Tenant")
        other = Company(name="Other SMS Tenant")
        db.session.add_all([company, other])
        db.session.flush()
        user = User(username="sms-route-admin", email="sms-route-admin@example.com", password_hash=generate_password_hash("password"), is_admin=True, default_company_id=company.id)
        db.session.add(user)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=user.id, company_id=company.id, role="admin", is_default=True))
        db.session.add(Contact(company_id=company.id, first_name="In", phone="+14155550001", tags="route-vip", sms_marketing_opt_in=True, sms_consent_status="opted_in"))
        db.session.add(Segment(company_id=company.id, name="route-vip"))
        # Sender configuration is mandatory: an authorized tenant phone line.
        db.session.add(TwilioPhoneNumber(company_id=company.id, phone_number="+15550109999", is_active=True,
                                         sms_enabled=True, campaign_sender_enabled=True, is_primary=True))
        account = TwilioAccount(company_id=company.id, from_phone="+15550109999", is_active=True)
        account.set_account_sid("ACroute")
        account.set_auth_token("token")
        db.session.add(account)
        db.session.add(Contact(company_id=other.id, first_name="Out", phone="+14155550002", tags="route-vip", sms_marketing_opt_in=True, sms_consent_status="opted_in"))
        db.session.add(Segment(company_id=other.id, name="route-vip"))
        db.session.add(SMSTemplate(company_id=company.id, name="Tenant Template", message="Hi Reply STOP", is_active=True))
        db.session.add(SMSTemplate(company_id=other.id, name="Other Template", message="Nope Reply STOP", is_active=True))
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        yield app, client, company, other, user
        db.session.remove()
        db.drop_all()


def test_sms_legacy_nav_routes_redirect_and_create_loads():
    for app, client, company, _other, _user in _login_fixture():
        for url in ("/sms", "/sms-dashboard"):
            response = client.get(url, follow_redirects=False)
            assert response.status_code in (301, 302)
            assert response.headers["Location"].endswith("/app/sms-campaigns")

        response = client.get("/sms/create")
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Tenant Template" in body
        assert "Other Template" not in body
        assert "Internal Server Error" not in body


def test_sms_create_persists_tenant_user_and_calendar_api_includes_campaign():
    for app, client, company, other, user in _login_fixture():
        scheduled_at = datetime.utcnow() + timedelta(days=2)
        response = client.post(
            "/sms/create",
            data={
                "name": "Scheduled Route SMS",
                "message": "Hello tenant",
                "segment_ids": str(Segment.query.filter_by(company_id=company.id).one().id),
                "from_phone_number_id": str(TwilioPhoneNumber.query.filter_by(company_id=company.id).one().id),
                "send_option": "scheduled",
                "scheduled_date": scheduled_at.strftime("%Y-%m-%d"),
                "scheduled_time": scheduled_at.strftime("%H:%M"),
            },
            follow_redirects=False,
        )
        assert response.status_code in (302, 303)

        with app.app_context():
            campaign = SMSCampaign.query.filter_by(name="Scheduled Route SMS").one()
            assert campaign.company_id == company.id
            assert campaign.created_by_user_id == user.id
            assert campaign.status == "scheduled"
            assert campaign.estimated_recipient_count == 1
            assert "STOP" in campaign.message.upper()
            assert SMSCampaign.query.filter_by(company_id=other.id).count() == 0

        events = client.get(
            f"/api/calendar/events?start={(scheduled_at - timedelta(days=1)).date().isoformat()}&end={(scheduled_at + timedelta(days=3)).date().isoformat()}&types=sms"
        )
        assert events.status_code == 200
        payload = events.get_json()
        assert any(e["title"] == "Scheduled Route SMS" and e["extendedProps"]["type"] == "sms" for e in payload)
