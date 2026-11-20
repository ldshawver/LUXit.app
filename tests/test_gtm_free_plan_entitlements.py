"""T-8: self-serve workspaces are on the free plan until they subscribe.

Pre-SaaS workspaces (no company_plan_policy row) must keep full access.
"""
from datetime import datetime, timedelta

import pytest
from flask_login import FlaskLoginClient
from werkzeug.security import generate_password_hash

import services.entitlements as entitlements
from app import create_app
from extensions import db
from models import Company, CompanyPlanPolicy, Contact, User, UserCompanyAccess
from services.contact_audience import upsert_contact_from_source
from services.entitlements import PlanLimitReached, is_free_plan
from services.signup import create_account


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(entitlements, "FREE_PLAN_MAX_CONTACTS", 2)
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="plan-test", SERVER_NAME="localhost")
    app.test_client_class = FlaskLoginClient
    with app.app_context():
        db.create_all()
        legacy = Company(name="Legacy Co", billing_tier="free", billing_status="none")
        free = Company(name="Signup Co", billing_tier="free", billing_status="none", max_team_members=1)
        db.session.add_all([legacy, free]); db.session.flush()
        db.session.add(CompanyPlanPolicy(company_id=free.id, plan_source="self_serve"))
        users = {}
        for key, co in (("legacy", legacy), ("free", free)):
            u = User(username=f"{key}-owner", email=f"{key}@example.com",
                     password_hash=generate_password_hash("pw"), default_company_id=co.id)
            db.session.add(u); db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role="owner", is_default=True))
            users[key] = u
        db.session.commit()
        yield app, legacy, free, users
        db.session.remove(); db.drop_all()


def test_plan_status(ctx):
    _, legacy, free, _ = ctx
    assert is_free_plan(legacy.id) is False
    assert is_free_plan(free.id) is True
    free.billing_status = "active"; db.session.commit()
    assert is_free_plan(free.id) is False
    free.billing_status = "past_due"; free.grace_period_ends_at = datetime.utcnow() + timedelta(days=3); db.session.commit()
    assert is_free_plan(free.id) is False
    free.grace_period_ends_at = datetime.utcnow() - timedelta(days=1); db.session.commit()
    assert is_free_plan(free.id) is True
    free.billing_status = "canceled"; db.session.commit()
    assert is_free_plan(free.id) is True


def _add(company_id, phone):
    contact = upsert_contact_from_source(company_id, phone=phone, source_channel="manual")
    db.session.commit()
    return contact


def test_free_plan_contact_cap(ctx):
    _, legacy, free, _ = ctx
    first = _add(free.id, "+19168675301")
    _add(free.id, "+19168675302")
    # Matching an existing contact is never blocked.
    assert _add(free.id, "+19168675301").id == first.id
    with pytest.raises(PlanLimitReached):
        _add(free.id, "+19168675303")
    db.session.rollback()
    for n in range(4):
        _add(legacy.id, f"+1916867540{n}")
    assert Contact.query.filter_by(company_id=legacy.id).count() == 4


def test_free_plan_contact_cap_lifts_after_subscribing(ctx):
    _, _, free, _ = ctx
    _add(free.id, "+19168675301")
    _add(free.id, "+19168675302")
    free.billing_status = "active"; db.session.commit()
    assert _add(free.id, "+19168675303").company_id == free.id


def test_ai_blocked_for_free_plan_with_402(ctx, monkeypatch):
    app, _, _, users = ctx
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    calls = []
    monkeypatch.setattr("routes.OpenAI", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("OpenAI called")), raising=False)
    with app.test_client(user=users["free"]) as c:
        r = c.post("/api/agents/chat", json={"message": "hello", "agent_type": "marketing"})
    assert r.status_code == 402, r.data
    assert r.get_json()["code"] == "PLAN_LIMIT"
    assert calls == []


def test_ai_not_plan_gated_for_legacy_workspace(ctx, monkeypatch):
    app, _, _, users = ctx
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with app.test_client(user=users["legacy"]) as c:
        r = c.post("/api/agents/chat", json={"message": "hello", "agent_type": "marketing"})
    assert r.status_code != 402


def test_lux_agent_client_blocked_for_free_plan(ctx, monkeypatch):
    app, _, _, users = ctx
    from ai_agent import LUXAgent
    agent = LUXAgent()
    agent._client = object()  # even a cached client must not be handed out
    with app.test_request_context("/"):
        from flask_login import login_user
        login_user(users["free"])
        with pytest.raises(PlanLimitReached):
            agent._get_client()
        login_user(users["legacy"])
        assert agent._get_client() is agent._client


def test_signup_workspace_starts_on_free_plan(ctx):
    data = {"org_name": "New Co", "full_name": "Sam Lee", "email": "sam@new.example", "password": "correct-horse-1"}
    user, outcome = create_account(data, ip="127.0.0.1")
    db.session.commit()
    company = db.session.get(Company, user.default_company_id)
    assert company.max_team_members == 1
    assert db.session.get(CompanyPlanPolicy, company.id).plan_source == "self_serve"
    assert is_free_plan(company.id) is True
    assert not user.is_admin
