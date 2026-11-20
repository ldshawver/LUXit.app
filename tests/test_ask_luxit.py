"""Ask LUXit (M6): tenant-safe questions with source-grounded answers."""
import uuid
from datetime import datetime, timedelta

import pytest
from flask import g
from flask.testing import FlaskClient

from app import create_app
from extensions import db
from models import (
    Company, Contact, ContactTask, EmailMessage, Opportunity, RelationshipMemory, TwilioConversation, TwilioMessage,
    User, UserCompanyAccess,
)
from services import ask_luxit


class _FreshUserClient(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


NOW = datetime.utcnow()


@pytest.fixture
def env():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost", RATELIMIT_ENABLED=False)
    app.test_client_class = _FreshUserClient
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        a, b = Company(name=f"Ask A {tag}"), Company(name=f"Ask B {tag}")
        db.session.add_all([a, b])
        db.session.flush()
        ids = {"a": a.id, "b": b.id, "tag": tag}
        for key, company, role in (("editor", a, "editor"), ("viewer", a, "viewer"), ("other", b, "owner")):
            u = User(username=f"ask-{key}-{tag}", email=f"ask-{key}-{tag}@example.com", password_hash="x",
                     default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            ids[key] = u.id
        nouser = User(username=f"ask-none-{tag}", email=f"ask-none-{tag}@example.com", password_hash="x")
        db.session.add(nouser)
        alice = Contact(company_id=a.id, first_name="Alice", last_name="Quarry", phone="+19168675341", is_active=True,
                        last_contacted_at=NOW - timedelta(days=1))
        bob = Contact(company_id=a.id, first_name="Bob", last_name="Stone", phone="+19168675342", is_active=True,
                      last_contacted_at=NOW - timedelta(days=90), lifecycle_stage="customer")
        dnc = Contact(company_id=a.id, first_name="Dee", phone="+19168675344", is_active=True, do_not_contact=True,
                      last_contacted_at=NOW - timedelta(days=200))
        carol = Contact(company_id=b.id, first_name="Alice", last_name="Other", phone="+19168675343", is_active=True,
                        last_contacted_at=NOW - timedelta(days=120))
        db.session.add_all([alice, bob, dnc, carol])
        db.session.flush()
        ids.update(alice=alice.id, bob=bob.id, dnc=dnc.id, carol=carol.id, nouser=nouser.id)

        conv = TwilioConversation(company_id=a.id, contact_id=alice.id, from_number=alice.phone, to_number="+19165550100")
        conv_b = TwilioConversation(company_id=b.id, contact_id=carol.id, from_number=carol.phone, to_number="+19165550199")
        db.session.add_all([conv, conv_b])
        db.session.flush()
        msg = TwilioMessage(conversation_id=conv.id, company_id=a.id, direction="inbound",
                            body="Can you send the pergola quote by Friday?", created_at=NOW - timedelta(hours=3))
        msg_b = TwilioMessage(conversation_id=conv_b.id, company_id=b.id, direction="inbound",
                              body="Tenant B pergola secret", created_at=NOW - timedelta(hours=2))
        db.session.add_all([msg, msg_b])
        db.session.flush()
        ids.update(msg=msg.id, msg_b=msg_b.id)

        db.session.add_all([
            ContactTask(company_id=a.id, contact_id=bob.id, title="Call Bob about renewal", status="open",
                        assigned_user_id=ids["editor"], due_at=NOW - timedelta(days=2)),
            ContactTask(company_id=b.id, contact_id=carol.id, title="B overdue task", status="open",
                        due_at=NOW - timedelta(days=2)),
            Opportunity(company_id=a.id, contact_id=bob.id, name="Pergola build", stage="proposal", status="open",
                        estimated_value=12000, probability=50, owner_user_id=ids["editor"]),
            Opportunity(company_id=b.id, contact_id=carol.id, name="B deal", stage="proposal", status="open",
                        estimated_value=99999, probability=90),
            RelationshipMemory(company_id=a.id, contact_id=alice.id, kind="commitment",
                               text="I'll send the pergola quote", due_at=NOW - timedelta(days=1),
                               source_refs=[{"table": "twilio_message", "id": msg.id}]),
            RelationshipMemory(company_id=b.id, contact_id=carol.id, kind="commitment", text="B promise"),
            EmailMessage(company_id=a.id, contact_id=bob.id, direction="inbound", subject="Pergola stain colours",
                         body_text="Prefer walnut", occurred_at=NOW - timedelta(days=5), provider="manual"),
        ])
        db.session.commit()
        yield app, ids
        db.session.remove()


def _as(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def _ask(app, uid, question):
    r = _as(app, uid).post("/api/ask", json={"question": question})
    return r.status_code, r.get_json()


def _assert_grounded(data, company_id):
    """Every cited number points at a result, and every result is a row of ``company_id``."""
    n = len(data["results"])
    for s in data["sentences"]:
        assert all(1 <= ref <= n for ref in s["refs"]), s
    from models import Contact
    for r in data["results"]:
        if r["contact_id"]:
            assert db.session.get(Contact, r["contact_id"]).company_id == company_id
        model = {
            "twilio_message": TwilioMessage, "contact_task": ContactTask, "opportunity": Opportunity,
            "relationship_memory": RelationshipMemory, "email_message": EmailMessage, "contact": Contact,
        }.get(r["source"]["table"])
        if model is not None:
            assert db.session.get(model, r["source"]["id"]).company_id == company_id


def test_unanswered_texts_are_cited_and_tenant_scoped(env):
    app, ids = env
    status, data = _ask(app, ids["viewer"], "Which texts are waiting for a reply?")
    assert status == 200 and data["intent"] == "unanswered_texts"
    assert data["answer"].startswith("1 text is waiting for a reply") and "[1]" in data["answer"]
    assert [r["contact_id"] for r in data["results"]] == [ids["alice"]]
    _assert_grounded(data, ids["a"])


def test_keyword_search_never_crosses_tenants(env):
    app, ids = env
    status, data = _ask(app, ids["editor"], "pergola")
    assert status == 200 and data["intent"] == "search"
    tables = {(r["source"]["table"], r["source"]["id"]) for r in data["results"]}
    assert ("twilio_message", ids["msg"]) in tables
    assert ("twilio_message", ids["msg_b"]) not in tables
    assert {"opportunity", "relationship_memory", "email_message"} <= {t for t, _ in tables}
    assert "secret" not in str(data) and "B deal" not in str(data)
    _assert_grounded(data, ids["a"])

    status, data_b = _ask(app, ids["other"], "pergola")
    assert [(r["source"]["table"], r["source"]["id"]) for r in data_b["results"]] == [("twilio_message", ids["msg_b"])]
    _assert_grounded(data_b, ids["b"])


def test_person_question_uses_that_contacts_history(env):
    app, ids = env
    status, data = _ask(app, ids["editor"], "What did Alice say about the quote?")
    assert data["intent"] == "contact"
    assert data["results"][0]["source"] == {"table": "twilio_message", "id": ids["msg"]}
    assert "'quote'" in data["answer"]
    _assert_grounded(data, ids["a"])

    # Tenant B also has an "Alice": B's answer is about B's Alice only.
    status, data_b = _ask(app, ids["other"], "Tell me about Alice")
    assert data_b["intent"] == "contact"
    assert {r["contact_id"] for r in data_b["results"]} == {ids["carol"]}
    _assert_grounded(data_b, ids["b"])


def test_person_briefing_without_topic(env):
    app, ids = env
    status, data = _ask(app, ids["editor"], "Tell me about Alice Quarry")
    assert data["intent"] == "contact"
    assert "You last heard from Alice Quarry" in data["answer"]
    assert any(s["text"].startswith("Suggested next step") for s in data["sentences"])
    _assert_grounded(data, ids["a"])


def test_tasks_pipeline_quiet_and_commitments(env):
    app, ids = env
    _, tasks = _ask(app, ids["editor"], "What are my overdue tasks?")
    assert tasks["intent"] == "tasks" and tasks["answer"].startswith("You have 1 overdue task")
    assert tasks["results"][0]["title"] == "Call Bob about renewal"
    _, viewer_tasks = _ask(app, ids["viewer"], "What are my overdue tasks?")
    assert viewer_tasks["answer"].startswith("You have no overdue tasks")

    _, pipe = _ask(app, ids["editor"], "Show open deals in proposal")
    assert pipe["intent"] == "pipeline" and "$12,000" in pipe["answer"] and "99,999" not in str(pipe)

    _, quiet = _ask(app, ids["editor"], "Who haven't we contacted in 60 days?")
    assert quiet["intent"] == "quiet_contacts"
    assert [r["contact_id"] for r in quiet["results"]] == [ids["bob"]]  # do-not-contact and other tenant excluded

    _, owed = _ask(app, ids["editor"], "What commitments do we owe?")
    assert owed["intent"] == "commitments" and "past due" in owed["answer"]
    assert "B promise" not in str(owed)
    for d in (tasks, pipe, quiet, owed):
        _assert_grounded(d, ids["a"])


def test_wildcards_are_literal_and_unknown_terms_say_nothing_found(env):
    app, ids = env
    _, data = _ask(app, ids["editor"], "%%")
    assert data["results"] == [] and data["answer"]
    _, data = _ask(app, ids["editor"], "zzqx_nonexistent")
    assert data["results"] == [] and "Nothing in this workspace matches" in data["answer"]


def test_access_and_validation(env):
    app, ids = env
    assert _as(app, ids["viewer"]).get("/ask").status_code == 200
    assert _ask(app, ids["editor"], "")[0] == 400
    assert _ask(app, ids["editor"], "x" * 501)[0] == 400
    assert _ask(app, ids["nouser"], "pergola")[0] == 404
    assert app.test_client().post("/api/ask", json={"question": "pergola"}).status_code in (302, 401)


def test_service_refuses_missing_workspace():
    with pytest.raises(ask_luxit.AskError):
        ask_luxit.ask(None, None, "anything")
