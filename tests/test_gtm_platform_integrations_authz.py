"""Routes that act on platform-owned accounts are platform-admin only.

Regression: /api/twilio/send-sms (platform Twilio number), /api/outlook/*
(platform Microsoft 365 mailbox + calendar) and /api/airtable/* (platform
Airtable base) only required a login, so any user of any tenant — a viewer
included — could text or email anyone as LUXit, or read the platform inbox.
"""
import uuid
from unittest.mock import patch

import pytest

from app import create_app
from extensions import db
from models import Company, User, UserCompanyAccess

ROUTES = [
    ("post", "/api/twilio/send-sms", {"to": "+15555550100", "body": "hi"}),
    ("post", "/api/outlook/send-email", {"to": "x@example.com", "subject": "s", "html_body": "b"}),
    ("get", "/api/outlook/messages/recent", None),
    ("get", "/api/outlook/calendar/events", None),
    ("post", "/api/outlook/calendar/events", {"subject": "s", "start": "2026-01-01T00:00", "end": "2026-01-01T01:00"}),
    ("get", "/api/airtable/records?base_id=b&table_name=t", None),
    ("post", "/api/airtable/records", {"base_id": "b", "table_name": "t", "fields": {}}),
    ("patch", "/api/airtable/records/rec1", {"base_id": "b", "table_name": "t", "fields": {}}),
    ("delete", "/api/airtable/records/rec1?base_id=b&table_name=t", None),
    ("get", "/api/airtable/health", None),
    ("post", "/api/airtable/sync/lead/1", {}),
    ("post", "/api/airtable/sync/onboarding/1", {}),
    ("post", "/api/airtable/sync/support/1", {}),
    ("get", "/api/airtable/sync/logs", None),
    ("get", "/api/airtable/sync/stats", None),
    ("get", "/platform/integrations/airtable", None),
]


@pytest.fixture
def env():
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    with app.app_context():
        tag = uuid.uuid4().hex[:10]
        company = Company(name=f"Integrations authz {tag}")
        db.session.add(company)
        db.session.flush()
        owner = User(username=f"int-owner-{tag}", email=f"int-owner-{tag}@example.com",
                     password_hash="x", default_company_id=company.id)
        db.session.add(owner)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=owner.id, company_id=company.id, role="owner", is_default=True))
        db.session.commit()
        yield app, owner.id


@pytest.mark.parametrize("method,url,body", ROUTES, ids=[f"{m} {u}" for m, u, _ in ROUTES])
def test_tenant_owner_cannot_use_platform_accounts(env, method, url, body):
    app, owner_id = env
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(owner_id)
        s["_fresh"] = True
    with patch("services.integrations.twilio_service.send_sms") as sms, \
         patch("services.integrations.outlook_service.send_email") as mail:
        r = getattr(c, method)(url, json=body) if body is not None else getattr(c, method)(url)
    assert r.status_code == 403, (url, r.status_code)
    assert sms.call_count == 0 and mail.call_count == 0


def test_unsigned_legacy_inbound_webhook_is_rejected(monkeypatch, env):
    app, _ = env
    monkeypatch.setenv("TWILIO_STRICT_SIGNATURE", "1")
    with patch("services.integrations.twilio_service.handle_inbound") as handle:
        r = app.test_client().post("/api/twilio/inbound-sms", data={"From": "+15555550100", "Body": "START"})
    # 400 (CSRF) or 403 (signature) — either way nothing is processed.
    assert r.status_code in (400, 403)
    assert handle.call_count == 0
