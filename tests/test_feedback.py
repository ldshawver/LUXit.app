"""Tests for the Feedback & Bug Reporting module.

Covers:
  - POST /api/feedback              — submit (auth required, validation)
  - GET  /feedback                  — user's own list (tenant isolation)
  - GET  /feedback/admin            — platform admin sees all; company admin
                                     sees only their company's tickets;
                                     regular user is redirected.
  - POST /api/feedback/<id>/status  — admin only; submitter forbidden
  - POST /api/feedback/<id>/assign  — admin only
  - POST /api/feedback/<id>/priority — admin only
  - POST /api/feedback/<id>/comment — visibility-checked; is_internal hidden
                                     from non-admins
"""
import pytest

from app import create_app
from extensions import db as _db
from models import (
    Company, FeedbackTicket, FeedbackTicketComment, User, UserCompanyAccess,
)


@pytest.fixture
def app():
    a = create_app()
    a.config.update(TESTING=True, SERVER_NAME="localhost",
                    WTF_CSRF_ENABLED=False, LOGIN_DISABLED=False)
    yield a


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def _seed_ticket(app, *, user_id, company_id, title, ticket_type="bug",
                 severity="medium", status="new"):
    """Insert a feedback ticket directly via the DB (no HTTP).

    Use this in visibility/auth tests where what matters is who can SEE a
    ticket, not who SUBMITTED it via the API. Avoids the Flask-Login
    session-switching gymnastics required to act as multiple users in one test.
    """
    with app.app_context():
        t = FeedbackTicket(user_id=user_id, company_id=company_id,
                           ticket_type=ticket_type, title=title,
                           description="seeded", severity=severity,
                           status=status)
        _db.session.add(t)
        _db.session.commit()
        return t.id


@pytest.fixture
def world(app):
    """Two companies, two users per company, plus a platform admin.

    co_a: alice (admin of co_a, regular user otherwise), bob (regular)
    co_b: carol (admin of co_b)
    plat: dave (platform admin, no company)
    """
    with app.app_context():
        UserCompanyAccess.query.delete()
        FeedbackTicketComment.query.delete()
        FeedbackTicket.query.delete()
        User.query.filter(User.username.in_(["alice", "bob", "carol", "dave"])).delete()
        Company.query.filter(Company.name.in_(["FB Co A", "FB Co B"])).delete()
        _db.session.commit()

        co_a = Company(name="FB Co A")
        co_b = Company(name="FB Co B")
        _db.session.add_all([co_a, co_b])
        _db.session.flush()

        alice = User(username="alice", email="alice@a.com", password_hash="x",
                     is_admin=False, default_company_id=co_a.id)
        bob   = User(username="bob",   email="bob@a.com",   password_hash="x",
                     is_admin=False, default_company_id=co_a.id)
        carol = User(username="carol", email="carol@b.com", password_hash="x",
                     is_admin=False, default_company_id=co_b.id)
        dave  = User(username="dave",  email="dave@plat.com", password_hash="x",
                     is_admin=True)
        _db.session.add_all([alice, bob, carol, dave])
        _db.session.flush()

        _db.session.add_all([
            UserCompanyAccess(user_id=alice.id, company_id=co_a.id,
                              role=UserCompanyAccess.ROLE_ADMIN),
            UserCompanyAccess(user_id=bob.id,   company_id=co_a.id,
                              role=UserCompanyAccess.ROLE_VIEWER),
            UserCompanyAccess(user_id=carol.id, company_id=co_b.id,
                              role=UserCompanyAccess.ROLE_ADMIN),
        ])
        _db.session.commit()

        yield {
            "co_a": co_a.id, "co_b": co_b.id,
            "alice": alice.id, "bob": bob.id,
            "carol": carol.id, "dave": dave.id,
        }


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _submit(client, **overrides):
    payload = {
        "ticket_type": "bug", "title": "Login is broken",
        "description": "Repro steps...", "severity": "high",
        "page_url": "/dashboard",
    }
    payload.update(overrides)
    return client.post("/api/feedback", data=payload)


# ── Submit ──────────────────────────────────────────────────────────────────

def test_submit_requires_auth(client):
    resp = _submit(client)
    assert resp.status_code in (302, 401)


def test_submit_creates_ticket_with_company_scope(app, client, world):
    _login(client, world["bob"])
    resp = _submit(client, title="Pagination broken")
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["ok"] and body["ticket"]["status"] == "new"
    with app.app_context():
        t = _db.session.get(FeedbackTicket, body["ticket"]["id"])
        assert t.user_id == world["bob"]
        assert t.company_id == world["co_a"]
        assert t.severity == "high"
        assert t.user_agent  # captured from headers


@pytest.mark.parametrize("field", ["title", "description"])
def test_submit_rejects_missing_required_field(client, world, field):
    _login(client, world["alice"])
    resp = _submit(client, **{field: ""})
    assert resp.status_code == 400


def test_submit_rejects_unknown_type(client, world):
    _login(client, world["alice"])
    resp = _submit(client, ticket_type="garbage")
    assert resp.status_code == 400


def test_submit_rejects_unknown_severity(client, world):
    _login(client, world["alice"])
    resp = _submit(client, severity="extreme")
    assert resp.status_code == 400


# ── Tenant isolation: my list ───────────────────────────────────────────────

def test_my_tickets_only_shows_own(app, client, world):
    _seed_ticket(app, user_id=world["bob"],   company_id=world["co_a"], title="Bob ticket")
    _seed_ticket(app, user_id=world["alice"], company_id=world["co_a"], title="Alice ticket")
    _login(client, world["bob"])
    resp = client.get("/feedback")
    assert resp.status_code == 200
    assert b"Bob ticket" in resp.data
    assert b"Alice ticket" not in resp.data


# ── Admin dashboard visibility ──────────────────────────────────────────────

def test_admin_dashboard_redirects_regular_user(client, world):
    _login(client, world["bob"])
    resp = client.get("/feedback/admin")
    # Bob is a viewer on co_a, not an admin → redirect to /feedback
    assert resp.status_code in (302, 303)
    assert "/feedback" in resp.headers.get("Location", "")


def test_company_admin_sees_only_own_company(app, client, world):
    """Carol (admin of co_b) should NOT see co_a's tickets."""
    _seed_ticket(app, user_id=world["bob"],   company_id=world["co_a"], title="Co-A ticket from Bob")
    _seed_ticket(app, user_id=world["carol"], company_id=world["co_b"], title="Co-B ticket from Carol")
    _login(client, world["carol"])
    resp = client.get("/feedback/admin")
    assert resp.status_code == 200
    assert b"Co-B ticket" in resp.data
    assert b"Co-A ticket" not in resp.data


def test_platform_admin_sees_all(app, client, world):
    _seed_ticket(app, user_id=world["bob"],   company_id=world["co_a"], title="Co-A from Bob")
    _seed_ticket(app, user_id=world["carol"], company_id=world["co_b"], title="Co-B from Carol")
    _login(client, world["dave"])
    resp = client.get("/feedback/admin")
    assert resp.status_code == 200
    assert b"Co-A from Bob" in resp.data
    assert b"Co-B from Carol" in resp.data


def test_admin_dashboard_filters_by_status(app, client, world):
    _login(client, world["alice"])
    r = _submit(client, title="Filterable")
    tid = r.get_json()["ticket"]["id"]
    # Alice (co_a admin) marks it in_progress.
    resp = client.post(f"/api/feedback/{tid}/status", json={"status": "in_progress"})
    assert resp.status_code == 200
    # Filter to "new" should hide it.
    resp = client.get("/feedback/admin?status=new")
    assert b"Filterable" not in resp.data
    resp = client.get("/feedback/admin?status=in_progress")
    assert b"Filterable" in resp.data


# ── Detail / authorization ──────────────────────────────────────────────────

def test_ticket_detail_blocks_other_tenant_user(app, client, world):
    tid = _seed_ticket(app, user_id=world["bob"], company_id=world["co_a"],
                       title="Bob private")
    _login(client, world["carol"])
    resp = client.get(f"/feedback/{tid}")
    # Forbidden flow → redirected to user's own list.
    assert resp.status_code in (302, 303)


def test_ticket_detail_allowed_for_own_company_admin(app, client, world):
    tid = _seed_ticket(app, user_id=world["bob"], company_id=world["co_a"],
                       title="Visible to Alice")
    _login(client, world["alice"])  # admin of same company
    resp = client.get(f"/feedback/{tid}")
    assert resp.status_code == 200
    assert b"Visible to Alice" in resp.data


# ── Status / assign / priority ──────────────────────────────────────────────

def test_status_change_blocked_for_submitter_only(app, client, world):
    """Bob is a viewer-only on co_a; he submits, but cannot change status."""
    _login(client, world["bob"])
    r = _submit(client, title="No-touch")
    tid = r.get_json()["ticket"]["id"]
    resp = client.post(f"/api/feedback/{tid}/status", json={"status": "closed"})
    assert resp.status_code == 403


def test_status_change_rejects_unknown_status(client, world):
    _login(client, world["alice"])
    r = _submit(client, title="X")
    tid = r.get_json()["ticket"]["id"]
    resp = client.post(f"/api/feedback/{tid}/status", json={"status": "wat"})
    assert resp.status_code == 400


def test_status_change_to_closed_sets_closed_at(app, client, world):
    _login(client, world["alice"])
    r = _submit(client, title="To close")
    tid = r.get_json()["ticket"]["id"]
    resp = client.post(f"/api/feedback/{tid}/status", json={"status": "closed"})
    assert resp.status_code == 200
    with app.app_context():
        assert _db.session.get(FeedbackTicket, tid).closed_at is not None


def test_priority_toggle_promotes_new_to_priority_fix(app, client, world):
    _login(client, world["alice"])
    r = _submit(client, title="P1")
    tid = r.get_json()["ticket"]["id"]
    resp = client.post(f"/api/feedback/{tid}/priority", json={"priority_fix": True})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["priority_fix"] is True and body["status"] == "priority_fix"


def test_assign_rejects_user_outside_company(app, client, world):
    _login(client, world["alice"])
    r = _submit(client, title="Try to assign across tenants")
    tid = r.get_json()["ticket"]["id"]
    # Carol is in co_b, ticket is in co_a.
    resp = client.post(f"/api/feedback/{tid}/assign",
                       json={"assigned_to_user_id": world["carol"]})
    assert resp.status_code == 400


def test_assign_accepts_same_company_user(app, client, world):
    _login(client, world["alice"])
    r = _submit(client, title="Assign to Bob")
    tid = r.get_json()["ticket"]["id"]
    resp = client.post(f"/api/feedback/{tid}/assign",
                       json={"assigned_to_user_id": world["bob"]})
    assert resp.status_code == 200


# ── Comments ────────────────────────────────────────────────────────────────

# ── Screenshot upload security ──────────────────────────────────────────────

def _png_bytes() -> bytes:
    """Smallest valid PNG (1x1 transparent) — for upload tests."""
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )


def test_upload_rejects_non_image_with_image_extension(app, client, world):
    """A renamed .exe (or any non-image bytes) must be rejected by magic-byte
    validation — extension-only checks are not enough."""
    from io import BytesIO
    _login(client, world["bob"])
    fake = BytesIO(b"MZ\x90\x00\x03" + b"A" * 200)  # Windows PE header
    resp = client.post("/api/feedback", data={
        "ticket_type": "bug", "title": "Evil upload",
        "description": "x", "severity": "high",
        "screenshot": (fake, "evil.png"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 201  # ticket still created
    tid = resp.get_json()["ticket"]["id"]
    with app.app_context():
        assert _db.session.get(FeedbackTicket, tid).screenshot_path is None


def test_upload_accepts_real_png_and_serves_via_authed_route(app, client, world):
    from io import BytesIO
    _login(client, world["bob"])
    resp = client.post("/api/feedback", data={
        "ticket_type": "bug", "title": "With shot",
        "description": "x", "severity": "high",
        "screenshot": (BytesIO(_png_bytes()), "shot.png"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 201
    tid = resp.get_json()["ticket"]["id"]
    with app.app_context():
        rel = _db.session.get(FeedbackTicket, tid).screenshot_path
        assert rel and rel.startswith(f"{tid}/") and rel.endswith(".png")
        # Confirm file lives under instance_path/uploads, NOT /static.
        from feedback import _screenshot_storage_root
        import os
        assert os.path.exists(os.path.join(_screenshot_storage_root(), rel))

    # Owner can fetch via authed route.
    r = client.get(f"/feedback/{tid}/screenshot")
    assert r.status_code == 200
    assert r.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_screenshot_route_requires_view_permission(app, client, world):
    """Carol (different tenant) must NOT fetch a co_a ticket's screenshot —
    permission denial returns 404 (no info leak about ticket existence)."""
    tid = _seed_ticket(app, user_id=world["bob"], company_id=world["co_a"],
                       title="Private shot")
    # Attach a screenshot path to the ticket directly (no file needed; the
    # permission check fires before the filesystem read).
    with app.app_context():
        t = _db.session.get(FeedbackTicket, tid)
        t.screenshot_path = f"{tid}/decoy.png"
        _db.session.commit()
    _login(client, world["carol"])
    resp = client.get(f"/feedback/{tid}/screenshot")
    assert resp.status_code == 404


def test_internal_note_hidden_from_submitter(app, client, world):
    tid = _seed_ticket(app, user_id=world["bob"], company_id=world["co_a"],
                       title="Comments test")
    # Seed both comments directly: one public from Bob, one internal from Alice.
    with app.app_context():
        _db.session.add_all([
            FeedbackTicketComment(ticket_id=tid, author_user_id=world["bob"],
                                  body="from bob", is_internal=False),
            FeedbackTicketComment(ticket_id=tid, author_user_id=world["alice"],
                                  body="ADMIN-ONLY-NOTE", is_internal=True),
        ])
        _db.session.commit()
    # Bob (submitter, viewer) must NOT see internal notes.
    _login(client, world["bob"])
    resp = client.get(f"/feedback/{tid}")
    assert resp.status_code == 200
    assert b"ADMIN-ONLY-NOTE" not in resp.data
    assert b"from bob" in resp.data

    # And via the API: Bob trying to post an internal comment is silently
    # downgraded to a public one.
    resp = client.post(f"/api/feedback/{tid}/comment",
                       json={"body": "another from bob", "is_internal": True})
    assert resp.status_code in (200, 201)
    with app.app_context():
        c = FeedbackTicketComment.query.filter_by(body="another from bob").first()
        assert c is not None and c.is_internal is False
