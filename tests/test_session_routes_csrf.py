"""Session-authenticated routes that change data require the CSRF token.

Regression: the whole /api/marketing blueprint (SMS/email campaign send,
contact merge, keywords, auto-replies) and the deal / workspace switch /
workspace leave / default-hub routes were @csrf.exempt. SameSite=Lax does
not cover sibling subdomains (dev./staging.luxit.app are same-site).
Also: leaving a workspace could deactivate it; closing is now the owner's
closure flow only.
"""
import re
import uuid

import pytest

from app import create_app
from extensions import db
from models import Company, Deal, User, UserCompanyAccess, user_company


@pytest.fixture
def env():
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=True)
    with app.app_context():
        tag = uuid.uuid4().hex[:10]
        company = Company(name=f"Csrf2 {tag}")
        other = Company(name=f"Csrf2 other {tag}")
        db.session.add_all([company, other])
        db.session.flush()
        users = {}
        for key, role in (("owner", "owner"), ("viewer", "viewer")):
            u = User(username=f"c2-{key}-{tag}", email=f"c2-{key}-{tag}@example.com", password_hash="x",
                     default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            db.session.execute(user_company.insert().values(user_id=u.id, company_id=company.id))
            users[key] = u.id
        db.session.add(UserCompanyAccess(user_id=users["owner"], company_id=other.id, role="owner"))
        deal = Deal(company_id=company.id, name=f"D {tag}")
        db.session.add(deal)
        db.session.commit()
        yield app, company.id, other.id, deal.id, users


def _client(app, uid):
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return client


def _post(app, client, url, token=None, **kw):
    from flask import g
    g.pop("_login_user", None)
    g.pop("csrf_token", None)
    headers = {"X-CSRFToken": token} if token else {}
    return client.post(url, headers=headers, **kw)


def _token(app, client):
    from flask import g
    g.pop("_login_user", None)
    g.pop("csrf_token", None)
    html = client.get("/app/billing").get_data(as_text=True)
    return re.search(r'<meta name="csrf-token" content="([^"]+)"', html).group(1)


ROUTES = [
    "/api/marketing/sms-campaigns",
    "/api/marketing/sms-campaigns/{deal}/send",
    "/api/marketing/campaigns/{deal}/send",
    "/api/marketing/contacts/duplicates/merge",
    "/api/marketing/sms-keywords",
    "/crm/deals/{deal}/update-stage",
    "/crm/deals/{deal}/delete",
    "/crm/deals/{deal}/activity",
    "/crm/deals/{deal}/update",
    "/companies/switch/{other}",
    "/companies/delete/{company}",
    "/api/user/set-default-hub",
]


@pytest.mark.parametrize("route", ROUTES)
def test_formerly_exempt_routes_refuse_a_missing_token(env, route):
    app, company_id, other_id, deal_id, users = env
    url = route.format(deal=deal_id, other=other_id, company=company_id)
    r = _post(app, _client(app, users["owner"]), url, json={"stage": "won"})
    assert r.status_code == 400, (url, r.status_code)
    assert r.get_json()["code"] == "csrf_failed"
    assert db.session.get(Deal, deal_id) is not None


def test_deal_stage_update_works_with_the_page_token(env):
    app, company_id, other_id, deal_id, users = env
    client = _client(app, users["owner"])
    r = _post(app, client, f"/crm/deals/{deal_id}/update-stage", token=_token(app, client), json={"stage": "won"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]


def test_last_owner_cannot_leave_and_leaving_never_deactivates(env):
    app, company_id, other_id, deal_id, users = env
    owner = _client(app, users["owner"])
    r = _post(app, owner, f"/companies/delete/{company_id}", token=_token(app, owner))
    assert r.status_code == 409 and r.get_json()["code"] == "last_owner"
    viewer = _client(app, users["viewer"])
    r = _post(app, viewer, f"/companies/delete/{company_id}", token=_token(app, viewer))
    assert r.status_code == 200, (r.status_code, r.headers.get("Location"), r.get_data(as_text=True)[:200])
    assert db.session.get(Company, company_id).is_active is not False
    assert UserCompanyAccess.query.filter_by(user_id=users["viewer"], company_id=company_id).count() == 0
