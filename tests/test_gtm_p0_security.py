"""Regression tests for the P0 security defects found in the 2026-10-19
go-to-market readiness audit (docs/readiness/LAUNCH_READINESS.md).

Each test fails on 645dd65 and passes with the fix.
"""
import re
from datetime import datetime

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import (
    Automation, Campaign, Company, Contact, Deal, PasswordResetToken, SMSCampaign,
    User, UserCompanyAccess,
)


@pytest.fixture
def world(monkeypatch):
    sent = []

    def fake_send(self, to_email, subject, html_content, from_email=None):
        sent.append({"to": to_email, "subject": subject, "html": html_content})
        return True

    monkeypatch.setattr("email_service.EmailService.send_email", fake_send)

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        a = Company(name="Tenant A")
        b = Company(name="Tenant B")
        db.session.add_all([a, b])
        db.session.flush()

        def mk(username, company, role, is_admin=False, **extra):
            u = User(username=username, email=f"{username}@example.com", is_admin=is_admin,
                     default_company_id=company.id, password_hash=generate_password_hash("old-password"))
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True, **extra))
            return u

        platform = mk("platform", a, "owner", is_admin=True)
        owner_a = mk("owner-a", a, "owner")
        viewer_a = mk("viewer-a", a, "viewer")
        staff_a = mk("staff-a", a, "staff")
        owner_b = mk("owner-b", b, "owner")

        contact_b = Contact(company_id=b.id, first_name="Bea", last_name="Private", email="bea@b.example", phone="+15555550100")
        deal_b = Deal(company_id=b.id, name="B secret deal", stage="proposal", value=9999)
        auto_b = Automation(company_id=b.id, name="B automation", is_active=True, status="active")
        camp_b = Campaign(company_id=b.id, name="B newsletter", subject="B subject", status="draft")
        sms_b = SMSCampaign(company_id=b.id, name="B sms blast", message="hello", status="draft")
        db.session.add_all([contact_b, deal_b, auto_b, camp_b, sms_b])
        db.session.commit()

        ids = dict(a=a.id, b=b.id, platform=platform.id, owner_a=owner_a.id, viewer_a=viewer_a.id,
                   staff_a=staff_a.id, owner_b=owner_b.id, contact_b=contact_b.id, deal_b=deal_b.id,
                   auto_b=auto_b.id, camp_b=camp_b.id, sms_b=sms_b.id)

        def client_for(user_id):
            c = app.test_client()
            if user_id is not None:
                with c.session_transaction() as sess:
                    sess["_user_id"] = str(user_id)
                    sess["_fresh"] = True
            return c

        yield app, client_for, ids, sent
        db.session.remove()
        db.drop_all()


# ── Unauthenticated account takeover ─────────────────────────────────────────

def test_forgot_password_never_shows_reset_link(world):
    app, client_for, ids, sent = world
    resp = client_for(None).post("/auth/forgot-password", data={"identifier": "platform"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "/auth/reset-password/" not in body
    with app.app_context():
        token = PasswordResetToken.query.filter_by(user_id=ids["platform"]).one().token
    assert token not in body
    assert len(sent) == 1 and sent[0]["to"] == "platform@example.com"
    assert token in sent[0]["html"]


def test_forgot_password_response_does_not_reveal_whether_account_exists(world):
    _, client_for, _, sent = world
    known = client_for(None).post("/auth/forgot-password", data={"identifier": "owner-a"}).get_data(as_text=True)
    unknown = client_for(None).post("/auth/forgot-password", data={"identifier": "nobody-here"}).get_data(as_text=True)
    strip = lambda html: re.sub(r'name="csrf_token" value="[^"]*"', "", html)
    assert strip(known) == strip(unknown)
    assert len(sent) == 1


def test_forgot_password_invalidates_older_links(world):
    app, client_for, ids, _ = world
    client_for(None).post("/auth/forgot-password", data={"identifier": "owner-a"})
    client_for(None).post("/auth/forgot-password", data={"identifier": "owner-a"})
    with app.app_context():
        rows = PasswordResetToken.query.filter_by(user_id=ids["owner_a"]).all()
        assert len(rows) == 2
        assert sum(1 for r in rows if not r.used) == 1


def test_password_reset_revokes_existing_sessions(world):
    app, client_for, ids, sent = world
    client_for(None).post("/auth/forgot-password", data={"identifier": "owner-a"})
    token = re.search(r"reset-password/([A-Za-z0-9_\-]+)", sent[0]["html"]).group(1)
    resp = client_for(None).post(f"/auth/reset-password/{token}", data={"password": "brand-new-pw", "confirm": "brand-new-pw"})
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(User, ids["owner_a"]).session_revoked_at is not None


def test_forgot_username_never_shows_username(world):
    _, client_for, _, sent = world
    body = client_for(None).post("/auth/forgot-username", data={"email": "owner-a@example.com"}).get_data(as_text=True)
    assert "owner-a" not in body.replace("owner-a@example.com", "")
    assert len(sent) == 1 and "owner-a" in sent[0]["html"]


# ── Privilege escalation ─────────────────────────────────────────────────────

def test_viewer_cannot_create_platform_admin_via_legacy_route(world):
    app, client_for, ids, _ = world
    client_for(ids["viewer_a"]).post("/user/add-user", data={
        "username": "evil", "email": "evil@example.com", "password": "pw123456",
        "confirm_password": "pw123456", "is_admin": "on"}, follow_redirects=True)
    with app.app_context():
        assert User.query.filter_by(username="evil").first() is None
        assert User.query.filter_by(is_admin=True).count() == 1


def test_viewer_cannot_add_users_via_canonical_route(world):
    app, client_for, ids, _ = world
    client_for(ids["viewer_a"]).post("/user/add", data={
        "username": "sneaky", "email": "sneaky@example.com", "password": "pw123456"})
    with app.app_context():
        assert User.query.filter_by(username="sneaky").first() is None


def test_owner_can_still_add_users(world):
    app, client_for, ids, _ = world
    client_for(ids["owner_a"]).post("/user/add", data={
        "username": "newhire", "email": "newhire@example.com", "password": "pw123456"})
    with app.app_context():
        u = User.query.filter_by(username="newhire").one()
        assert not u.is_admin
        assert UserCompanyAccess.query.filter_by(user_id=u.id, company_id=ids["a"]).one().role == "viewer"


def test_any_user_cannot_edit_other_users_via_legacy_route(world):
    app, client_for, ids, _ = world
    client_for(ids["viewer_a"]).post(f"/user/edit-user/{ids['owner_b']}", data={
        "username": "owner-b", "email": "attacker@example.com", "new_password": "hijacked1", "is_admin": "on"})
    with app.app_context():
        target = db.session.get(User, ids["owner_b"])
        assert target.email == "owner-b@example.com"
        assert not target.is_admin


def test_tenant_owner_cannot_edit_platform_admin(world):
    app, client_for, ids, _ = world
    client_for(ids["owner_a"]).post(f"/user/edit/{ids['platform']}", data={
        "username": "platform", "email": "attacker@example.com"})
    with app.app_context():
        assert db.session.get(User, ids["platform"]).email == "platform@example.com"


def test_tenant_owner_cannot_assign_unknown_role(world):
    app, client_for, ids, _ = world
    client_for(ids["owner_a"]).post(f"/user/edit/{ids['staff_a']}", data={
        "username": "staff-a", "email": "staff-a@example.com", "role": "god_mode"})
    with app.app_context():
        assert UserCompanyAccess.query.filter_by(user_id=ids["staff_a"], company_id=ids["a"]).one().role == "staff"


def test_platform_admin_editing_self_keeps_admin(world):
    app, client_for, ids, _ = world
    client_for(ids["platform"]).post(f"/user/edit/{ids['platform']}", data={
        "username": "platform", "email": "platform@example.com", "first_name": "Plat"})
    with app.app_context():
        assert db.session.get(User, ids["platform"]).is_admin


def test_profile_tags_do_not_grant_admin(world):
    app, client_for, ids, _ = world
    client_for(ids["viewer_a"]).put("/api/user/profile", json={"tags": ["admin"], "segment": "admin"})
    with app.app_context():
        assert db.session.get(User, ids["viewer_a"]).is_admin_user is False


# ── /saas operator console ───────────────────────────────────────────────────

def test_tenant_owner_cannot_open_saas_console(world):
    _, client_for, ids, _ = world
    assert client_for(ids["owner_a"]).get("/saas/").status_code == 403
    assert client_for(ids["owner_a"]).get("/saas/automation-log").status_code == 403


def test_tenant_user_cannot_change_any_company_subscription(world):
    app, client_for, ids, _ = world
    resp = client_for(ids["viewer_a"]).post(f"/saas/accounts/{ids['a']}/edit", data={
        "subscription_tier": "enterprise", "stripe_subscription_status": "active"})
    assert resp.status_code == 403
    with app.app_context():
        assert db.session.get(Company, ids["a"]).subscription_tier != "enterprise"


def test_platform_admin_keeps_saas_console(world):
    _, client_for, ids, _ = world
    assert client_for(ids["platform"]).get("/saas/").status_code == 200


# ── Cross-tenant object access (IDOR) ────────────────────────────────────────

def test_cannot_view_other_tenant_contact_activities(world):
    _, client_for, ids, _ = world
    resp = client_for(ids["owner_a"]).get(f"/contacts/{ids['contact_b']}/activities")
    assert resp.status_code == 404
    assert "bea@b.example" not in resp.get_data(as_text=True)


def test_cannot_add_activity_to_other_tenant_contact(world):
    app, client_for, ids, _ = world
    resp = client_for(ids["owner_a"]).post(f"/api/contacts/{ids['contact_b']}/activities/add", data={"subject": "x"})
    assert resp.status_code == 404
    with app.app_context():
        from models import ContactActivity
        assert ContactActivity.query.filter_by(contact_id=ids["contact_b"]).count() == 0


def test_cannot_toggle_or_delete_other_tenant_automation(world):
    app, client_for, ids, _ = world
    c = client_for(ids["owner_a"])
    c.post(f"/automations/{ids['auto_b']}/toggle")
    c.post(f"/automations/{ids['auto_b']}/pause")
    c.post(f"/automations/{ids['auto_b']}/delete")
    with app.app_context():
        auto = db.session.get(Automation, ids["auto_b"])
        assert auto is not None and auto.is_active and auto.status == "active"


def test_cannot_read_or_send_other_tenant_email_campaign(world):
    app, client_for, ids, _ = world
    c = client_for(ids["owner_a"])
    assert "B subject" not in c.get(f"/campaigns/{ids['camp_b']}").get_data(as_text=True)
    c.post(f"/campaigns/{ids['camp_b']}/send")
    with app.app_context():
        assert db.session.get(Campaign, ids["camp_b"]).status == "draft"


def test_cannot_read_other_tenant_sms_campaign_analytics(world):
    _, client_for, ids, _ = world
    assert client_for(ids["owner_a"]).get(f"/sms/{ids['sms_b']}/analytics").status_code == 404


def test_cannot_reschedule_other_tenant_campaigns_from_calendar(world):
    app, client_for, ids, _ = world
    c = client_for(ids["owner_a"])
    for ev in (f"sms_{ids['sms_b']}", f"email_{ids['camp_b']}"):
        assert c.patch(f"/api/calendar/events/{ev}", json={"start": "2030-01-01T09:00:00"}).status_code == 404
        assert c.get(f"/api/calendar/events/{ev}").status_code == 404
    with app.app_context():
        assert db.session.get(SMSCampaign, ids["sms_b"]).scheduled_at is None
        assert db.session.get(Campaign, ids["camp_b"]).scheduled_at is None


def test_unhandled_error_does_not_persist_partial_writes(world):
    app, client_for, ids, _ = world

    @app.route("/__boom_after_write")
    def _boom():
        u = db.session.get(User, ids["viewer_a"])
        u.email = "half-written@example.com"
        raise RuntimeError("boom")

    resp = client_for(ids["viewer_a"]).get("/__boom_after_write")
    assert resp.status_code == 500
    db.session.expire_all()
    assert db.session.get(User, ids["viewer_a"]).email == "viewer-a@example.com"


def test_edit_form_preselects_role_and_plain_save_keeps_it(world):
    app, client_for, ids, _ = world
    c = client_for(ids["owner_a"])
    page = c.get(f"/user/edit/{ids['staff_a']}").get_data(as_text=True)
    assert '<option value="staff" selected>' in page
    # Saving the form as rendered (role submitted unchanged) must not demote.
    c.post(f"/user/edit/{ids['staff_a']}", data={"username": "staff-a", "email": "staff-a@example.com", "role": "staff"})
    with app.app_context():
        assert UserCompanyAccess.query.filter_by(user_id=ids["staff_a"], company_id=ids["a"]).one().role == "staff"


# ── Password reset token handling ────────────────────────────────────────────

def _reset_token(client_for, sent, identifier):
    client_for(None).post("/auth/forgot-password", data={"identifier": identifier})
    return re.search(r"reset-password/([A-Za-z0-9_\-]+)", sent[-1]["html"]).group(1)


def test_reset_link_works_once(world):
    app, client_for, ids, sent = world
    token = _reset_token(client_for, sent, "owner-a")
    c = client_for(None)
    c.post(f"/auth/reset-password/{token}", data={"password": "first-new-pw", "confirm": "first-new-pw"})
    second = c.post(f"/auth/reset-password/{token}", data={"password": "attacker-pw1", "confirm": "attacker-pw1"})
    assert "first-new-pw" not in second.get_data(as_text=True)
    from werkzeug.security import check_password_hash
    with app.app_context():
        h = db.session.get(User, ids["owner_a"]).password_hash
        assert check_password_hash(h, "first-new-pw") and not check_password_hash(h, "attacker-pw1")


def test_expired_or_unknown_reset_link_is_refused(world):
    app, client_for, ids, sent = world
    token = _reset_token(client_for, sent, "owner-a")
    with app.app_context():
        from datetime import timedelta
        row = PasswordResetToken.query.filter_by(token=token).one()
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
    for t in (token, "not-a-real-token"):
        client_for(None).post(f"/auth/reset-password/{t}", data={"password": "attacker-pw1", "confirm": "attacker-pw1"})
    from werkzeug.security import check_password_hash
    with app.app_context():
        assert check_password_hash(db.session.get(User, ids["owner_a"]).password_hash, "old-password")


# ── Role escalation through the access API ───────────────────────────────────

def _grant_manage_users(app, user_id, company_id):
    with app.app_context():
        acc = UserCompanyAccess.query.filter_by(user_id=user_id, company_id=company_id).one()
        acc.role = "admin"
        acc.manage_users_enabled = True
        db.session.commit()


def test_manage_users_admin_cannot_make_self_or_others_owner(world):
    app, client_for, ids, _ = world
    _grant_manage_users(app, ids["staff_a"], ids["a"])
    c = client_for(ids["staff_a"])
    assert c.post(f"/api/user/{ids['staff_a']}/access", json={"role": "owner"}).status_code == 403
    assert c.post(f"/api/user/{ids['viewer_a']}/access", json={"role": "owner"}).status_code == 403
    assert c.post(f"/api/user/{ids['owner_a']}/access", json={"role": "viewer"}).status_code == 403
    assert c.post(f"/api/user/{ids['platform']}/access", json={"role": "viewer"}).status_code == 403
    with app.app_context():
        roles = {u: UserCompanyAccess.query.filter_by(user_id=ids[u], company_id=ids["a"]).one().role
                 for u in ("staff_a", "viewer_a", "owner_a", "platform")}
    assert roles == {"staff_a": "admin", "viewer_a": "viewer", "owner_a": "owner", "platform": "owner"}


def test_manage_users_admin_can_still_change_ordinary_roles(world):
    app, client_for, ids, _ = world
    _grant_manage_users(app, ids["staff_a"], ids["a"])
    resp = client_for(ids["staff_a"]).post(f"/api/user/{ids['viewer_a']}/access", json={"role": "editor"})
    assert resp.status_code == 200
    with app.app_context():
        assert UserCompanyAccess.query.filter_by(user_id=ids["viewer_a"], company_id=ids["a"]).one().role == "editor"


def test_owner_can_grant_owner(world):
    app, client_for, ids, _ = world
    assert client_for(ids["owner_a"]).post(f"/api/user/{ids['staff_a']}/access", json={"role": "owner"}).status_code == 200


# ── Global admin pages ───────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/global-admin/billing", "/global-admin/licenses", "/api/global/billing/failed-payments"])
def test_tenant_owner_cannot_open_global_admin(world, path):
    _, client_for, ids, _ = world
    assert client_for(ids["owner_a"]).get(path).status_code == 403


# ── SMS campaign sends across tenants ────────────────────────────────────────

def test_cannot_send_other_tenant_sms_campaign(world):
    app, client_for, ids, _ = world
    c = client_for(ids["owner_a"])
    c.post(f"/sms/campaign/{ids['sms_b']}/send")
    assert c.post(f"/api/marketing/sms-campaigns/{ids['sms_b']}/send", json={}).status_code == 404
    with app.app_context():
        assert db.session.get(SMSCampaign, ids["sms_b"]).status == "draft"
