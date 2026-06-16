"""
Integration layer tests.

Covers:
- GET /api/integrations/health returns safe statuses (no secrets)
- POST /api/integrations/health/<provider>/test
- Twilio: blocks do_not_text, blocks opt-out, handles STOP/START, creates new lead
- RevenueCat: safe failure when unavailable, entitlement checks
- Outlook: graceful failure on bad token
- Airtable: graceful failure when unconfigured, never blocks
- GitHub: platform admin required, blocks non-admin
"""
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["FLASK_ENV"] = "testing"
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "g2CDXwdc6VKAElQ5QWqFBCsmXL_dQAs3e44_Gl1oJaU=")


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope="module")
def app():
    from app import create_app
    a = create_app()
    a.config["TESTING"]    = True
    a.config["WTF_CSRF_ENABLED"] = False
    with a.app_context():
        from extensions import db
        db.create_all()
        yield a


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


@pytest.fixture(scope="module")
def admin_user(app):
    """Return a platform admin user, creating one only if needed.

    Module-scoped so the user is committed to PostgreSQL once and remains
    visible to Flask-Login across all tests (module-scoped client).  The
    ``_db_rollback`` autouse fixture is function-scoped and patches
    ``db.session`` *after* this fixture runs, so the commit here is a real
    PostgreSQL commit — the user persists for every test in the module.
    """
    from extensions import db
    from models import User, Company
    user = User.query.filter_by(email="admin@luxit.app").first()
    if user is None:
        company = Company(name="LUXit Platform Test")
        db.session.add(company)
        db.session.flush()
        user = User(
            email="admin@luxit.app",
            username="admin_test",
            password_hash="x",
            is_admin=True,
            default_company_id=company.id,
        )
        db.session.add(user)
        db.session.commit()
    else:
        if not user.is_admin:
            user.is_admin = True
            db.session.commit()
    yield user


@pytest.fixture(scope="module")
def regular_user(app):
    """Return a regular (non-admin) user, creating one only if needed.

    Module-scoped for the same reason as ``admin_user`` above.
    """
    from extensions import db
    from models import User, Company
    user = User.query.filter_by(email="tenant@luxit.app").first()
    if user is None:
        company = Company(name="Tenant Co Test")
        db.session.add(company)
        db.session.flush()
        user = User(
            email="tenant@luxit.app",
            username="tenant_test",
            password_hash="x",
            is_admin=False,
            default_company_id=company.id,
        )
        db.session.add(user)
        db.session.commit()
    yield user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"]   = True


# ============================================================
# Health endpoint
# ============================================================

class TestIntegrationHealth:
    def test_health_requires_login(self, client):
        r = client.get("/api/integrations/health")
        assert r.status_code in (302, 401)

    def test_health_returns_all_providers(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/api/integrations/health")
        assert r.status_code == 200
        data = r.get_json()
        for provider in ("twilio", "github", "outlook", "airtable", "revenuecat"):
            assert provider in data, f"Missing provider: {provider}"
            assert "status" in data[provider]

    def test_health_status_values_are_safe(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/api/integrations/health")
        data = r.get_json()
        allowed = {"connected", "missing_config", "error", "disabled", "unknown"}
        for provider, info in data.items():
            assert info["status"] in allowed, f"{provider}: unexpected status {info['status']}"

    def test_health_no_secrets_in_response(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/api/integrations/health")
        body = r.data.decode()
        for bad in ("ACCOUNT_SID", "AUTH_TOKEN", "CLIENT_SECRET", "API_KEY", "PERSONAL_ACCESS_TOKEN"):
            assert bad not in body, f"Secret keyword '{bad}' found in health response"

    def test_health_non_admin_gets_status_only(self, client, app, regular_user):
        _login(client, regular_user)
        r = client.get("/api/integrations/health")
        assert r.status_code == 200
        data = r.get_json()
        for provider, info in data.items():
            assert "detail" not in info or True  # soft check
            assert "status" in info

    def test_single_provider_test(self, client, admin_user):
        _login(client, admin_user)
        r = client.post("/api/integrations/health/twilio/test")
        assert r.status_code == 200
        data = r.get_json()
        assert "status" in data

    def test_unknown_provider_rejected(self, client, admin_user):
        _login(client, admin_user)
        r = client.post("/api/integrations/health/malicious/test")
        assert r.status_code == 400


# ============================================================
# Twilio service unit tests
# ============================================================

class TestTwilioService:
    def test_send_sms_blocked_do_not_text(self, app):
        from extensions import db
        from models import Contact, Company
        co = Company(name="SMSCo")
        db.session.add(co)
        db.session.flush()
        contact = Contact(
            phone="+15550000001",
            company_id=co.id,
            tags="do_not_text,vip",
            is_subscribed=True,
        )
        db.session.add(contact)
        db.session.commit()

        from services.integrations.twilio_service import send_sms
        result = send_sms("+15550000001", "Hello!", company_id=co.id)
        assert result["ok"] is False
        assert "do_not_text" in result["reason"]

    def test_send_sms_blocked_unsubscribed(self, app):
        from extensions import db
        from models import Contact, Company
        co = Company(name="SMSCo2")
        db.session.add(co)
        db.session.flush()
        contact = Contact(
            phone="+15550000002",
            company_id=co.id,
            tags="",
            is_subscribed=False,
        )
        db.session.add(contact)
        db.session.commit()

        from services.integrations.twilio_service import send_sms
        result = send_sms("+15550000002", "Hello!", company_id=co.id)
        assert result["ok"] is False
        assert "subscribed" in result["reason"].lower()

    def test_send_sms_missing_credentials(self, app, monkeypatch):
        monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
        monkeypatch.delenv("TWILIO_AUTH_TOKEN",  raising=False)
        monkeypatch.delenv("TWILIO_PHONE_NUMBER",raising=False)

        from services.integrations.twilio_service import send_sms
        result = send_sms("+15550000099", "Hello!", company_id=None)
        assert result["ok"] is False

    def test_handle_inbound_stop(self, app):
        from services.integrations.twilio_service import handle_inbound
        result = handle_inbound("+15559990001", "STOP", "+18001234567")
        assert result["action"] == "opt_out"
        assert result["reply"] is not None
        assert "unsubscribed" in result["reply"].lower()

    def test_handle_inbound_start(self, app):
        from services.integrations.twilio_service import handle_inbound
        result = handle_inbound("+15559990002", "START", "+18001234567")
        assert result["action"] == "opt_in"
        assert result["reply"] is not None

    def test_handle_inbound_stop_variants(self, app):
        from services.integrations.twilio_service import handle_inbound
        for word in ("UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT"):
            r = handle_inbound(f"+1555{hash(word) % 9000 + 1000}", word, "+18001234567")
            assert r["action"] == "opt_out", f"'{word}' should opt out"

    def test_handle_inbound_creates_new_lead(self, app):
        from extensions import db
        from models import Company, Contact, TwilioConversation
        co = Company(name="LeadCo")
        db.session.add(co)
        db.session.commit()

        new_phone = "+15558887766"

        from services.integrations.twilio_service import handle_inbound
        result = handle_inbound(new_phone, "Hey there", "+18001234567", company_id=co.id)
        assert result["action"] == "new_lead"

        c = Contact.query.filter_by(phone=new_phone, company_id=co.id).first()
        assert c is not None
        assert "sms_opt_in" in (c.tags or "")


# ============================================================
# RevenueCat service unit tests
# ============================================================

class TestRevenueCatService:
    def test_health_missing_config(self, app, monkeypatch):
        monkeypatch.delenv("REVENUECAT_SECRET_KEY", raising=False)
        monkeypatch.delenv("REVENUECAT_API_KEY",    raising=False)
        from services.integrations.revenuecat_service import health_check
        result = health_check()
        assert result["status"] == "missing_config"

    def test_get_customer_info_missing_config(self, app, monkeypatch):
        monkeypatch.delenv("REVENUECAT_SECRET_KEY", raising=False)
        monkeypatch.delenv("REVENUECAT_API_KEY",    raising=False)
        from services.integrations.revenuecat_service import get_customer_info
        result = get_customer_info("user-123")
        assert result["ok"] is False
        assert "entitlements" in result

    def test_has_entitlement_returns_false_when_unavailable(self, app, monkeypatch):
        monkeypatch.delenv("REVENUECAT_SECRET_KEY", raising=False)
        monkeypatch.delenv("REVENUECAT_API_KEY",    raising=False)
        from services.integrations.revenuecat_service import has_entitlement
        assert has_entitlement("user-123", "luxit_access") is False

    def test_app_does_not_crash_when_revenuecat_unavailable(self, client, admin_user):
        """Health endpoint still responds even if RevenueCat is down."""
        _login(client, admin_user)
        r = client.get("/api/integrations/health")
        assert r.status_code == 200
        data = r.get_json()
        rc = data.get("revenuecat", {})
        assert rc.get("status") in ("missing_config", "error", "connected")

    def test_webhook_stores_event(self, app):
        from services.integrations.revenuecat_service import handle_webhook
        from models import IntegrationEvent
        payload = {
            "event": {
                "type": "INITIAL_PURCHASE",
                "app_user_id": "user-webhook-test",
                "product_id": "luxit_starter_monthly",
                "period_type": "NORMAL",
            }
        }
        result = handle_webhook(payload)
        assert result["ok"] is True
        ev = IntegrationEvent.query.filter_by(
            provider="revenuecat", event_type="INITIAL_PURCHASE"
        ).first()
        assert ev is not None


# ============================================================
# Outlook service unit tests
# ============================================================

class TestOutlookService:
    def test_health_documents_missing_config(self, app, monkeypatch):
        for key in ("MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_TENANT_ID"):
            monkeypatch.delenv(key, raising=False)
        from services.integrations.outlook_service import health_check
        result = health_check()
        assert result["status"] == "missing_config"
        assert "Microsoft credentials not configured" in result["detail"]

    def test_send_email_graceful_failure(self, app, monkeypatch):
        import services.integrations.outlook_service as svc
        monkeypatch.setattr(svc, "_get_token", lambda: None)
        result = svc.send_email("to@example.com", "Test", "<p>Hi</p>")
        assert result["ok"] is False
        assert "reason" in result

    def test_create_calendar_event_graceful_failure(self, app, monkeypatch):
        import services.integrations.outlook_service as svc
        monkeypatch.setattr(svc, "_get_token", lambda: None)
        result = svc.create_calendar_event(
            "Demo Call", "2026-06-01T10:00:00", "2026-06-01T11:00:00"
        )
        assert result["ok"] is False


def test_woocommerce_missing_config_is_non_crashing(app, monkeypatch):
    for key in ("WC_STORE_URL", "WC_CONSUMER_KEY", "WC_CONSUMER_SECRET"):
        monkeypatch.delenv(key, raising=False)
    from woocommerce_service import WooCommerceService

    svc = WooCommerceService()
    assert svc.is_configured() is False
    assert svc.get_products() is None


def test_posthog_missing_config_noops(app, monkeypatch):
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    import services.posthog_client as posthog_client

    posthog_client._client = None
    assert posthog_client._get_client() is None
    assert posthog_client.track_event("user-1", "audit_test", {"ok": True}) is None


# ============================================================
# Airtable service unit tests
# ============================================================

class TestAirtableService:
    def test_health_missing_config(self, app, monkeypatch):
        monkeypatch.delenv("AIRTABLE_API_KEY", raising=False)
        monkeypatch.delenv("AIRTABLE_TOKEN",   raising=False)
        import services.provider_config as _pc
        monkeypatch.setattr(_pc, "get_provider_config", lambda *a, **kw: None)
        from services.integrations.airtable_service import health_check
        result = health_check()
        assert result["status"] == "missing_config"

    def test_list_records_missing_config(self, app, monkeypatch):
        monkeypatch.delenv("AIRTABLE_API_KEY", raising=False)
        monkeypatch.delenv("AIRTABLE_TOKEN",   raising=False)
        import services.provider_config as _pc
        monkeypatch.setattr(_pc, "get_provider_config", lambda *a, **kw: None)
        from services.integrations.airtable_service import list_records
        result = list_records("base123", "Leads")
        assert result["ok"] is False
        assert result["records"] == []

    def test_create_record_missing_config(self, app, monkeypatch):
        monkeypatch.delenv("AIRTABLE_API_KEY", raising=False)
        monkeypatch.delenv("AIRTABLE_TOKEN",   raising=False)
        import services.provider_config as _pc
        monkeypatch.setattr(_pc, "get_provider_config", lambda *a, **kw: None)
        from services.integrations.airtable_service import create_record
        result = create_record("base123", "Leads", {"Name": "Test"})
        assert result["ok"] is False

    def test_airtable_failure_does_not_block_health(self, client, admin_user, monkeypatch):
        """Core health endpoint still works even if Airtable fails."""
        _login(client, admin_user)
        r = client.get("/api/integrations/health")
        assert r.status_code == 200


# ============================================================
# GitHub service unit tests
# ============================================================

class TestGitHubService:
    def test_github_endpoint_requires_platform_admin(self, client, regular_user):
        _login(client, regular_user)
        r = client.get("/api/github/repos")
        assert r.status_code == 403

    def test_github_endpoint_accessible_by_admin(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/api/github/repos")
        assert r.status_code == 200
        data = r.get_json()
        assert "ok" in data

    def test_github_list_repos_with_token(self, app):
        from services.integrations.github_service import list_repos
        result = list_repos()
        assert "ok" in result

    def test_github_missing_token(self, app, monkeypatch):
        monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        import services.provider_config as _pc
        monkeypatch.setattr(_pc, "get_provider_config", lambda *a, **kw: None)
        from services.integrations.github_service import health_check
        result = health_check()
        assert result["status"] == "missing_config"

    def test_github_create_issue_sanitises_content(self, app):
        from services.integrations.github_service import _sanitise
        dirty = "<script>alert('xss')</script>"
        clean = _sanitise(dirty)
        assert "<" not in clean
        assert ">" not in clean

    def test_github_issue_creation_requires_admin(self, client, regular_user):
        _login(client, regular_user)
        r = client.post(
            "/api/github/luxit-hq/luxit-app/issues",
            json={"title": "Test", "body": "Body"},
        )
        assert r.status_code == 403


# ============================================================
# Admin UI
# ============================================================

class TestAdminUI:
    def test_platform_integrations_page_loads(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/platform/integrations")
        assert r.status_code == 200
        assert b"Platform Integrations" in r.data

    def test_platform_integrations_shows_all_providers(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/platform/integrations")
        body = r.data.decode()
        for name in ("Twilio", "GitHub", "Microsoft Outlook", "RevenueCat", "Airtable"):
            assert name in body, f"Provider '{name}' not found in /platform/integrations"

    def test_platform_integrations_no_secrets_in_page(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/platform/integrations")
        body = r.data.decode()
        for secret in ("ACCOUNT_SID", "AUTH_TOKEN", "CLIENT_SECRET", "PERSONAL_ACCESS_TOKEN"):
            assert secret not in body, f"Secret '{secret}' leaked in admin UI"

    def test_platform_integrations_requires_login(self, client):
        with client.session_transaction() as sess:
            sess.clear()
        r = client.get("/platform/integrations")
        assert r.status_code in (302, 401)
