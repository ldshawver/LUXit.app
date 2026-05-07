"""
Airtable sync integration tests.

Covers:
  - Health endpoint returns connected/missing_config
  - Sync disabled returns safe message
  - Sync lead: success, tenant violation, not found, Airtable outage
  - Sync onboarding: success, tenant violation
  - Sync support: success, tenant violation
  - Failure logs to ExternalSyncRecord with sync_status=failed
  - Airtable outage does NOT crash the app
  - Sync logs API returns JSON
  - /platform/integrations/airtable page loads
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "g2CDXwdc6VKAElQ5QWqFBCsmXL_dQAs3e44_Gl1oJaU=")


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope="module")
def app():
    from app import create_app
    a = create_app()
    a.config["TESTING"] = True
    a.config["WTF_CSRF_ENABLED"] = False
    with a.app_context():
        from extensions import db
        db.create_all()
        yield a


@pytest.fixture(scope="module")
def client(app):
    return app.test_client()


@pytest.fixture
def company(app):
    with app.app_context():
        from extensions import db
        from models import Company
        co = Company(name="AirtableTestCo")
        db.session.add(co)
        db.session.commit()
        yield co
        db.session.delete(co)
        db.session.commit()


@pytest.fixture
def admin_user(app, company):
    with app.app_context():
        from extensions import db
        from models import User
        u = User(
            email="airtable_admin@luxit.app",
            username="airtable_admin",
            password_hash="x",
            is_admin=True,
            default_company_id=company.id,
        )
        db.session.add(u)
        db.session.commit()
        yield u
        db.session.delete(u)
        db.session.commit()


@pytest.fixture
def contact(app, company):
    with app.app_context():
        from extensions import db
        from models import Contact
        c = Contact(
            email="lead@example.com",
            first_name="Ada",
            last_name="Lovelace",
            phone="+15550001111",
            company_id=company.id,
            source="test",
            is_active=True,
            is_subscribed=True,
            tags="vip,test",
        )
        db.session.add(c)
        db.session.commit()
        yield c
        db.session.delete(c)
        db.session.commit()


@pytest.fixture
def onboarding_project(app, company):
    with app.app_context():
        from extensions import db
        from models import CustomerOnboardingProject
        p = CustomerOnboardingProject(
            company_id=company.id,
            title="Test Onboarding",
            status="in_progress",
            notes="Integration test project",
        )
        db.session.add(p)
        db.session.commit()
        yield p
        db.session.delete(p)
        db.session.commit()


@pytest.fixture
def feedback_ticket(app, company, admin_user):
    with app.app_context():
        from extensions import db
        from models import FeedbackTicket
        t = FeedbackTicket(
            title="Test bug",
            description="Something broke",
            ticket_type="bug",
            severity="medium",
            status="new",
            user_id=admin_user.id,
            company_id=company.id,
        )
        db.session.add(t)
        db.session.commit()
        yield t
        db.session.delete(t)
        db.session.commit()


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _mock_airtable_success(external_id="recABC123"):
    """Return a mock requests.Response for a successful Airtable create/patch."""
    mock = MagicMock()
    mock.status_code = 200
    mock.content = b'{"id": "recABC123"}'
    mock.json.return_value = {"id": external_id}
    return mock


def _mock_airtable_error(status=500):
    mock = MagicMock()
    mock.status_code = status
    mock.content = b'{"error": {"message": "Server error"}}'
    mock.json.return_value = {"error": {"message": "Server error"}}
    mock.text = "Server error"
    return mock


# ============================================================
# Health
# ============================================================

class TestAirtableHealth:
    def test_health_endpoint_requires_login(self, client):
        r = client.get("/api/airtable/health")
        assert r.status_code in (302, 401)

    def test_health_returns_status(self, client, admin_user):
        _login(client, admin_user)
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"tables": [{"name": "Leads"}, {"name": "Onboarding Pipeline"}]},
                content=b'{}',
            )
            r = client.get("/api/airtable/health")
        assert r.status_code == 200
        data = r.get_json()
        assert "status" in data
        assert data["status"] in ("connected", "missing_config", "error")

    def test_health_missing_api_key(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.delenv("AIRTABLE_API_KEY", raising=False)
            monkeypatch.delenv("AIRTABLE_TOKEN", raising=False)
            from services.integrations.airtable_service import health_check
            result = health_check()
            assert result["status"] == "missing_config"

    def test_health_connected_when_api_responds(self, app):
        with app.app_context():
            from services.integrations.airtable_service import health_check
            with patch("requests.get") as mock_get:
                mock_get.return_value = MagicMock(
                    status_code=200,
                    json=lambda: {"tables": [{"name": "Leads"}]},
                    content=b'{}',
                )
                result = health_check()
            assert result["status"] == "connected"
            assert result["sync_enabled"] is True
            assert result["tables_found"] == 1


# ============================================================
# Sync enabled/disabled gate
# ============================================================

class TestSyncGate:
    def test_sync_disabled_returns_safe_message(self, app, contact, monkeypatch):
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "false")
            from services.integrations import airtable_service
            result = airtable_service.sync_lead_to_airtable(contact.id, contact.company_id)
            assert result["ok"] is False
            assert "disabled" in result["reason"].lower()

    def test_sync_missing_config_returns_safe(self, app, contact, monkeypatch):
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            monkeypatch.delenv("AIRTABLE_API_KEY", raising=False)
            monkeypatch.delenv("AIRTABLE_TOKEN", raising=False)
            from services.integrations import airtable_service
            result = airtable_service.sync_lead_to_airtable(contact.id, contact.company_id)
            assert result["ok"] is False
            assert "configured" in result["reason"].lower()


# ============================================================
# Lead sync
# ============================================================

class TestLeadSync:
    def test_sync_lead_success(self, app, contact, monkeypatch):
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            from services.integrations import airtable_service
            with patch("requests.request") as mock_req:
                mock_req.return_value = _mock_airtable_success("recLEAD001")
                result = airtable_service.sync_lead_to_airtable(contact.id, contact.company_id)

            assert result["ok"] is True
            assert result["external_id"] == "recLEAD001"
            assert result["action"] == "created"

            # Sync record persisted
            from models import ExternalSyncRecord
            row = ExternalSyncRecord.query.filter_by(
                provider="airtable",
                local_entity_type="contact",
                local_entity_id=str(contact.id),
            ).first()
            assert row is not None
            assert row.sync_status == "synced"
            assert row.external_entity_id == "recLEAD001"

    def test_sync_lead_updates_existing(self, app, contact, monkeypatch):
        """Second sync should PATCH, not POST."""
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            from services.integrations import airtable_service
            with patch("requests.request") as mock_req:
                mock_req.return_value = _mock_airtable_success("recLEAD001")
                # First call
                airtable_service.sync_lead_to_airtable(contact.id, contact.company_id)
                # Second call — should PATCH
                result = airtable_service.sync_lead_to_airtable(contact.id, contact.company_id)

            assert result["ok"] is True
            assert result["action"] == "updated"
            # Verify PATCH was used on second call
            calls = mock_req.call_args_list
            assert any(c[0][0] == "PATCH" for c in calls), "Expected at least one PATCH call"

    def test_sync_lead_tenant_isolation(self, app, contact, monkeypatch):
        """sync with wrong company_id must be rejected."""
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            from services.integrations import airtable_service
            wrong_company_id = contact.company_id + 9999
            result = airtable_service.sync_lead_to_airtable(contact.id, wrong_company_id)
            assert result["ok"] is False
            assert "tenant" in result["reason"].lower() or "isolation" in result["reason"].lower()

    def test_sync_lead_not_found(self, app, company, monkeypatch):
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            from services.integrations import airtable_service
            result = airtable_service.sync_lead_to_airtable(999999, company.id)
            assert result["ok"] is False
            assert "not found" in result["reason"].lower()

    def test_sync_lead_airtable_outage_logs_failure(self, app, contact, monkeypatch):
        """Airtable outage: result is ok=False, sync_status=failed, app does not crash."""
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            # Clear existing sync row for clean test
            from extensions import db
            from models import ExternalSyncRecord
            ExternalSyncRecord.query.filter_by(
                provider="airtable",
                local_entity_type="contact",
                local_entity_id=str(contact.id),
                company_id=contact.company_id,
            ).delete()
            db.session.commit()

            from services.integrations import airtable_service
            with patch("requests.request", side_effect=ConnectionError("Airtable is down")):
                result = airtable_service.sync_lead_to_airtable(contact.id, contact.company_id)

            assert result["ok"] is False

            # Failure was logged
            row = ExternalSyncRecord.query.filter_by(
                provider="airtable",
                local_entity_type="contact",
                local_entity_id=str(contact.id),
                company_id=contact.company_id,
            ).first()
            assert row is not None
            assert row.sync_status == "failed"

    def test_sync_lead_via_api(self, client, admin_user, contact, monkeypatch):
        monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
        _login(client, admin_user)
        with patch("requests.request") as mock_req:
            mock_req.return_value = _mock_airtable_success("recAPI001")
            r = client.post(f"/api/airtable/sync/lead/{contact.id}")
        assert r.status_code == 200
        data = r.get_json()
        assert "ok" in data


# ============================================================
# Onboarding sync
# ============================================================

class TestOnboardingSync:
    def test_sync_onboarding_success(self, app, onboarding_project, monkeypatch):
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            from services.integrations import airtable_service
            with patch("requests.request") as mock_req:
                mock_req.return_value = _mock_airtable_success("recONB001")
                result = airtable_service.sync_onboarding_to_airtable(
                    onboarding_project.id, onboarding_project.company_id
                )
            assert result["ok"] is True
            assert result["external_id"] == "recONB001"

            from models import ExternalSyncRecord
            row = ExternalSyncRecord.query.filter_by(
                provider="airtable",
                local_entity_type="onboarding_project",
                local_entity_id=str(onboarding_project.id),
            ).first()
            assert row is not None
            assert row.sync_status == "synced"

    def test_sync_onboarding_tenant_isolation(self, app, onboarding_project, monkeypatch):
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            from services.integrations import airtable_service
            result = airtable_service.sync_onboarding_to_airtable(
                onboarding_project.id, onboarding_project.company_id + 8888
            )
            assert result["ok"] is False
            assert "tenant" in result["reason"].lower() or "isolation" in result["reason"].lower()

    def test_sync_onboarding_via_api(self, client, admin_user, onboarding_project, monkeypatch):
        monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
        _login(client, admin_user)
        with patch("requests.request") as mock_req:
            mock_req.return_value = _mock_airtable_success("recONBAPI")
            r = client.post(f"/api/airtable/sync/onboarding/{onboarding_project.id}")
        assert r.status_code == 200
        data = r.get_json()
        assert "ok" in data


# ============================================================
# Support note sync
# ============================================================

class TestSupportSync:
    def test_sync_support_success(self, app, feedback_ticket, monkeypatch):
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            from services.integrations import airtable_service
            with patch("requests.request") as mock_req:
                mock_req.return_value = _mock_airtable_success("recSUP001")
                result = airtable_service.sync_support_note_to_airtable(
                    feedback_ticket.id, feedback_ticket.company_id
                )
            assert result["ok"] is True
            assert result["external_id"] == "recSUP001"

            from models import ExternalSyncRecord
            row = ExternalSyncRecord.query.filter_by(
                provider="airtable",
                local_entity_type="feedback_ticket",
                local_entity_id=str(feedback_ticket.id),
            ).first()
            assert row is not None
            assert row.sync_status == "synced"

    def test_sync_support_tenant_isolation(self, app, feedback_ticket, monkeypatch):
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            from services.integrations import airtable_service
            result = airtable_service.sync_support_note_to_airtable(
                feedback_ticket.id, feedback_ticket.company_id + 7777
            )
            assert result["ok"] is False

    def test_sync_support_via_api(self, client, admin_user, feedback_ticket, monkeypatch):
        monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
        _login(client, admin_user)
        with patch("requests.request") as mock_req:
            mock_req.return_value = _mock_airtable_success("recSUPAPI")
            r = client.post(f"/api/airtable/sync/support/{feedback_ticket.id}")
        assert r.status_code == 200
        data = r.get_json()
        assert "ok" in data


# ============================================================
# Retry logic
# ============================================================

class TestRetryLogic:
    def test_retries_on_500(self, app, monkeypatch):
        """Three 500s should exhaust retries and return ok=False."""
        with app.app_context():
            from services.integrations.airtable_service import _request_with_retry
            with patch("requests.request") as mock_req, \
                 patch("time.sleep"):   # don't actually sleep in tests
                mock_req.return_value = _mock_airtable_error(500)
                result = _request_with_retry("POST", "https://example.com", "tok", body={})
            assert result["ok"] is False
            assert mock_req.call_count == 4  # initial + 3 retries

    def test_retries_on_429(self, app, monkeypatch):
        """Rate limit (429) should also be retried."""
        with app.app_context():
            from services.integrations.airtable_service import _request_with_retry
            with patch("requests.request") as mock_req, \
                 patch("time.sleep"):
                mock_req.return_value = _mock_airtable_error(429)
                result = _request_with_retry("GET", "https://example.com", "tok")
            assert result["ok"] is False
            assert mock_req.call_count == 4

    def test_no_retry_on_404(self, app):
        """404 is a client error — no retry."""
        with app.app_context():
            from services.integrations.airtable_service import _request_with_retry
            not_found = MagicMock()
            not_found.status_code = 404
            not_found.content = b'{"error":{"message":"Not found"}}'
            not_found.json.return_value = {"error": {"message": "Not found"}}
            not_found.text = "Not found"
            with patch("requests.request") as mock_req:
                mock_req.return_value = not_found
                result = _request_with_retry("GET", "https://example.com", "tok")
            assert result["ok"] is False
            assert mock_req.call_count == 1  # no retry

    def test_connection_error_retries(self, app):
        """ConnectionError should be retried."""
        with app.app_context():
            from services.integrations.airtable_service import _request_with_retry
            import requests as req_mod
            with patch("requests.request", side_effect=req_mod.exceptions.ConnectionError("down")), \
                 patch("time.sleep"):
                result = _request_with_retry("POST", "https://example.com", "tok", body={})
            assert result["ok"] is False


# ============================================================
# App resilience — Airtable outage does not crash app
# ============================================================

class TestAppResilience:
    def test_health_endpoint_works_when_airtable_down(self, client, admin_user):
        """Main integrations health should still respond even when Airtable is unreachable."""
        _login(client, admin_user)
        with patch("requests.get", side_effect=ConnectionError("Airtable down")), \
             patch("requests.request", side_effect=ConnectionError("Airtable down")):
            r = client.get("/api/integrations/health")
        assert r.status_code == 200
        data = r.get_json()
        assert "airtable" in data
        assert data["airtable"]["status"] in ("error", "missing_config")

    def test_sync_failure_does_not_raise(self, app, contact, monkeypatch):
        with app.app_context():
            monkeypatch.setenv("AIRTABLE_SYNC_ENABLED", "true")
            from services.integrations import airtable_service
            with patch("requests.request", side_effect=Exception("catastrophic failure")), \
                 patch("time.sleep"):
                result = airtable_service.sync_lead_to_airtable(contact.id, contact.company_id)
            assert isinstance(result, dict)
            assert result["ok"] is False

    def test_sync_logs_api_works_when_airtable_down(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/api/airtable/sync/logs")
        assert r.status_code == 200
        data = r.get_json()
        assert "logs" in data


# ============================================================
# Sync logs API
# ============================================================

class TestSyncLogsAPI:
    def test_sync_logs_requires_login(self, client):
        with client.session_transaction() as sess:
            sess.clear()
        r = client.get("/api/airtable/sync/logs")
        assert r.status_code in (302, 401)

    def test_sync_stats_returns_counts(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/api/airtable/sync/stats")
        assert r.status_code == 200
        data = r.get_json()
        for key in ("synced", "failed", "pending", "total"):
            assert key in data

    def test_sync_logs_returns_list(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/api/airtable/sync/logs")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data.get("logs"), list)


# ============================================================
# Admin UI
# ============================================================

class TestAirtableAdminUI:
    def test_airtable_detail_page_loads(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/platform/integrations/airtable")
        assert r.status_code == 200
        assert b"Airtable" in r.data

    def test_airtable_page_shows_sync_tables(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/platform/integrations/airtable")
        body = r.data.decode()
        for section in ("Leads", "Onboarding", "Support"):
            assert section in body, f"'{section}' section missing from Airtable admin page"

    def test_airtable_page_no_secrets(self, client, admin_user):
        _login(client, admin_user)
        r = client.get("/platform/integrations/airtable")
        body = r.data.decode()
        assert "AIRTABLE_API_KEY" not in body
        assert "Bearer " not in body

    def test_airtable_page_requires_login(self, client):
        with client.session_transaction() as sess:
            sess.clear()
        r = client.get("/platform/integrations/airtable")
        assert r.status_code in (302, 401)
