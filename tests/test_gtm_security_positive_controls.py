"""Positive controls for the P0 security fixes: the legitimate, authorized
path each fix touches must keep working. Pairs with test_gtm_p0_security.py,
which proves the exploit is blocked.
"""
import re

import pytest

from werkzeug.security import check_password_hash

from extensions import db
from models import Automation, Campaign, Company, Contact, SMSCampaign, User, UserCompanyAccess

from tests.test_gtm_p0_security import world  # noqa: F401  (shared fixture)


def _own_objects(ids):
    a = ids["a"]
    contact = Contact(company_id=a, first_name="Ann", last_name="Own", email="ann@a.example", phone="+15555550111")
    auto = Automation(company_id=a, name="A automation", is_active=True, status="active")
    camp = Campaign(company_id=a, name="A newsletter", subject="A subject", status="draft")
    sms = SMSCampaign(company_id=a, name="A sms", message="hi", status="draft")
    db.session.add_all([contact, auto, camp, sms])
    db.session.commit()
    return contact.id, auto.id, camp.id, sms.id


def test_reset_link_from_email_sets_new_password_and_user_can_log_in(world):
    app, client_for, ids, sent = world
    client_for(None).post("/auth/forgot-password", data={"identifier": "owner-a"})
    token = re.search(r"reset-password/([A-Za-z0-9_\-]+)", sent[0]["html"]).group(1)
    client_for(None).post(f"/auth/reset-password/{token}", data={"password": "brand-new-pw", "confirm": "brand-new-pw"})
    db.session.expire_all()
    assert check_password_hash(db.session.get(User, ids["owner_a"]).password_hash, "brand-new-pw")
    resp = client_for(None).post("/auth/login", data={"username": "owner-a", "password": "brand-new-pw"})
    assert resp.status_code in (302, 303)
    assert "/auth/login" not in resp.headers.get("Location", "")


def test_owner_sees_and_manages_own_tenant_objects(world):
    app, client_for, ids, _ = world
    contact_id, auto_id, camp_id, sms_id = _own_objects(ids)
    c = client_for(ids["owner_a"])
    # campaign_detail renders the list template with a plain list, which shows
    # no rows (pre-existing, also on 645dd65); the scoping signal is that the
    # own campaign is found rather than redirected as "not found".
    own = c.get(f"/campaigns/{camp_id}")
    assert own.status_code == 200 and "Campaign not found" not in own.get_data(as_text=True)
    assert c.get(f"/contacts/{contact_id}/activities").status_code == 200
    assert c.get(f"/sms/{sms_id}/analytics").status_code == 200
    # /pause 500s on Automation.pause (missing, pre-existing); toggle works.
    c.post(f"/automations/{auto_id}/toggle")
    db.session.expire_all()
    assert db.session.get(Automation, auto_id).is_active is False


def test_owner_can_add_activity_to_own_contact(world):
    app, client_for, ids, _ = world
    contact_id, *_ = _own_objects(ids)
    resp = client_for(ids["owner_a"]).post(f"/api/contacts/{contact_id}/activities/add",
                                           data={"activity_type": "note", "subject": "called"})
    assert resp.status_code in (200, 201, 302, 303)
    from models import ContactActivity
    assert ContactActivity.query.filter_by(contact_id=contact_id).count() == 1


def test_platform_admin_can_still_change_subscription(world):
    app, client_for, ids, _ = world
    resp = client_for(ids["platform"]).post(f"/saas/accounts/{ids['b']}/edit", data={"subscription_tier": "enterprise"})
    assert resp.status_code in (200, 302, 303)
    db.session.expire_all()
    assert db.session.get(Company, ids["b"]).subscription_tier == "enterprise"


def test_successful_request_still_commits(world):
    app, client_for, ids, _ = world

    @app.route("/__ok_write")
    def _ok():
        u = db.session.get(User, ids["viewer_a"])
        u.email = "committed@example.com"
        db.session.commit()
        return "ok"

    assert client_for(ids["viewer_a"]).get("/__ok_write").status_code == 200
    db.session.expire_all()
    assert db.session.get(User, ids["viewer_a"]).email == "committed@example.com"


def test_owner_can_edit_user_role_in_own_tenant(world):
    app, client_for, ids, _ = world
    client_for(ids["owner_a"]).post(f"/user/edit/{ids['staff_a']}",
                                    data={"username": "staff-a", "email": "staff-a@example.com", "role": "editor"})
    db.session.expire_all()
    access = UserCompanyAccess.query.filter_by(user_id=ids["staff_a"], company_id=ids["a"]).one()
    assert access.role == "editor"
