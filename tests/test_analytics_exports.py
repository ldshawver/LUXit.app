from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from lux.analytics.query_service import AnalyticsQueryService
from lux.extensions import db
from lux.models.analytics import ConsentSuppressed, RawEvent
from lux.models.user import User


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SERVER_NAME"] = "localhost"

    with app.app_context():
        db.create_all()
        user = User(
            username="lux",
            email="lux@example.com",
            password_hash=generate_password_hash("secret"),
            is_admin=True,
        )
        db.session.add(user)
        db.session.commit()

        with app.test_client() as client:
            with client.session_transaction() as session:
                session["_user_id"] = str(user.id)
                session["_fresh"] = True
            yield client
        db.session.remove()
        db.drop_all()


def test_csv_export_includes_summary(client):
    with client.application.app_context():
        db.session.add(
            RawEvent(
                company_id=1,
                event_name="page_view",
                occurred_at=datetime.now(timezone.utc),
            )
        )
        db.session.add(ConsentSuppressed(company_id=1, day=datetime.utcnow().date(), count=2))
        db.session.commit()

    response = client.get("/analytics/report/export/csv?company_id=1")
    assert response.status_code == 200
    assert "Total Events" in response.get_data(as_text=True)


def test_tenant_isolation_summary(client):
    with client.application.app_context():
        db.session.add(
            RawEvent(
                company_id=1,
                event_name="event",
                occurred_at=datetime.now(timezone.utc),
            )
        )
        db.session.add(
            RawEvent(
                company_id=2,
                event_name="event",
                occurred_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

        summary = AnalyticsQueryService.summary(1, datetime(2024, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc))
        assert summary["total_events"] == 1


def test_event_ingest_respects_consent(client):
    response = client.post(
        "/e",
        json={"company_id": 1, "event_name": "page_view", "consent": False},
    )
    assert response.status_code == 204
