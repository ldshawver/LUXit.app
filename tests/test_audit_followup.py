import os
from datetime import datetime

import pytest

from app import create_app
from extensions import db
from models import (
    AgentDeliverable,
    AgentLog,
    AgentReport,
    AgentTask,
    ApprovalQueue,
    Company,
    Notification,
    User,
    user_company,
)


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _tenant(name: str, username: str):
    company = Company(name=name)
    user = User(username=username, email=f"{username}@example.com", is_admin=True)
    user.set_password("pw")
    db.session.add_all([company, user])
    db.session.flush()
    user.default_company_id = company.id
    db.session.execute(
        user_company.insert().values(
            user_id=user.id,
            company_id=company.id,
            is_default=True,
        )
    )
    db.session.commit()
    return user, company


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def test_agent_reports_are_company_scoped_for_list_and_detail(client):
    user_a, company_a = _tenant("Agent Tenant A", "agent-a")
    _, company_b = _tenant("Agent Tenant B", "agent-b")
    report_a = AgentReport(
        company_id=company_a.id,
        agent_type="analytics",
        agent_name="Analytics",
        report_type="monthly",
        report_title="Tenant A Monthly",
        report_data={"summary": "A"},
        period_start=datetime.utcnow(),
        period_end=datetime.utcnow(),
    )
    report_b = AgentReport(
        company_id=company_b.id,
        agent_type="analytics",
        agent_name="Analytics",
        report_type="monthly",
        report_title="Tenant B Monthly",
        report_data={"summary": "B"},
        period_start=datetime.utcnow(),
        period_end=datetime.utcnow(),
    )
    db.session.add_all([report_a, report_b])
    db.session.commit()

    _login(client, user_a)
    listing = client.get("/api/agents/analytics/reports")
    assert listing.status_code == 200
    titles = {r["title"] for r in listing.get_json()["reports"]}
    assert titles == {"Tenant A Monthly"}

    allowed = client.get(f"/api/agents/analytics/reports/{report_a.id}")
    assert allowed.status_code == 200
    assert "Tenant A Monthly" == allowed.get_json()["report"]["title"]

    denied = client.get(f"/api/agents/analytics/reports/{report_b.id}")
    assert denied.status_code == 404

    denied_page = client.get(f"/agents/reports/{report_b.id}")
    assert denied_page.status_code == 404


def test_agent_activity_and_performance_are_company_scoped(client):
    user_a, company_a = _tenant("Agent Activity A", "agent-activity-a")
    _, company_b = _tenant("Agent Activity B", "agent-activity-b")
    db.session.add_all([
        AgentLog(company_id=company_a.id, agent_type="analytics", agent_name="Analytics", activity_type="ran", status="ok", details="A"),
        AgentLog(company_id=company_b.id, agent_type="analytics", agent_name="Analytics", activity_type="ran", status="ok", details="B"),
        AgentTask(company_id=company_a.id, agent_type="analytics", task_name="A", status="completed"),
        AgentTask(company_id=company_b.id, agent_type="analytics", task_name="B", status="failed"),
    ])
    db.session.commit()

    _login(client, user_a)
    activity = client.get("/api/agents/activity?agent_type=analytics")
    assert activity.status_code == 200
    assert [a["details"] for a in activity.get_json()["activities"]] == ["A"]

    perf = client.get("/api/agents/analytics/performance")
    assert perf.status_code == 200
    metrics = perf.get_json()["metrics"]
    assert metrics["total_tasks"] == 1
    assert metrics["completed_tasks"] == 1


def test_agent_deliverable_create_is_durable_and_company_scoped(client):
    user_a, company_a = _tenant("Deliverable Tenant A", "deliverable-a")
    _login(client, user_a)

    response = client.post(
        "/api/agents/content_seo/deliverables",
        json={
            "deliverable_type": "blog_draft",
            "description": "Draft a launch article",
            "priority": "high",
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True

    deliverable = db.session.get(AgentDeliverable, payload["deliverable_id"])
    assert deliverable is not None
    assert deliverable.company_id == company_a.id
    assert deliverable.requested_by_id == user_a.id
    assert deliverable.priority == "high"
    assert deliverable.status == "pending"


def test_approval_queue_is_company_scoped_for_list_and_actions(client):
    user_a, company_a = _tenant("Approval Tenant A", "approval-a")
    _, company_b = _tenant("Approval Tenant B", "approval-b")
    item_a = ApprovalQueue(
        company_id=company_a.id,
        content_type="social_post",
        title="Tenant A Post",
        content_preview="A",
        status="pending_review",
    )
    item_b = ApprovalQueue(
        company_id=company_b.id,
        content_type="social_post",
        title="Tenant B Post",
        content_preview="B",
        status="pending_review",
    )
    db.session.add_all([item_a, item_b])
    db.session.commit()

    _login(client, user_a)
    listing = client.get("/api/approval-queue")
    assert listing.status_code == 200
    titles = {item["title"] for item in listing.get_json()["items"]}
    assert titles == {"Tenant A Post"}

    denied_detail = client.get(f"/api/approval-queue/{item_b.id}")
    assert denied_detail.status_code == 404

    denied_approve = client.post(f"/api/approval-queue/{item_b.id}/approve", json={"notes": "nope"})
    assert denied_approve.status_code == 404


def test_notifications_mark_read_is_user_isolated(client):
    user_a, company_a = _tenant("Notify Tenant A", "notify-a")
    user_b, company_b = _tenant("Notify Tenant B", "notify-b")
    notification_b = Notification(
        user_id=user_b.id,
        company_id=company_b.id,
        title="Tenant B only",
        message="private",
    )
    db.session.add(notification_b)
    db.session.commit()

    _login(client, user_a)
    response = client.post(
        "/notifications/mark-read",
        data={"notification_id": str(notification_b.id)},
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}
    db.session.refresh(notification_b)
    assert notification_b.is_read is False
