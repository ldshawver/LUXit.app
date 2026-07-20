import pytest
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import Company, Contact, Opportunity, User


@pytest.fixture
def audience_app():
    from app import create_app
    app = create_app()
    app.config.update(TESTING=False, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield app


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def _tenant_user():
    company = Company(name="Audience Co", is_active=True)
    user = User(username="audience", email="audience@example.com", active=True)
    db.session.add_all([company, user])
    db.session.flush()
    user.default_company_id = company.id
    db.session.commit()
    return user, company


def test_authenticated_contacts_empty_state_returns_200(audience_app):
    client = audience_app.test_client()
    with audience_app.app_context():
        user, _company = _tenant_user()
        _login(client, user.id)
        resp = client.get("/contacts")
        assert resp.status_code == 200
        assert "Add Your First Contact" in resp.get_data(as_text=True)


def test_unauthenticated_contacts_redirects_to_login(audience_app):
    resp = audience_app.test_client().get("/contacts", follow_redirects=False)
    assert resp.status_code in {302, 303}
    assert "/auth/login" in resp.headers["Location"]


def test_contacts_company_scope_search_filters_pagination_and_archived(audience_app):
    client = audience_app.test_client()
    with audience_app.app_context():
        user, company = _tenant_user()
        other = Company(name="Other Co", is_active=True)
        db.session.add(other)
        db.session.flush()
        own_contact = Contact(company_id=company.id, email="alpha@example.com", first_name="Alpha", is_active=True, archived_at=None, tags="vip", lifecycle_stage="lead")
        other_contact = Contact(company_id=other.id, email="alpha-other@example.com", first_name="AlphaOther", is_active=True)
        db.session.add_all([
            own_contact,
            Contact(company_id=company.id, email="arch@example.com", first_name="Archived", is_active=False, archived_at=db.func.now()),
            other_contact,
        ])
        db.session.flush()
        db.session.add_all([
            Opportunity(company_id=company.id, contact_id=own_contact.id, name="Own", status="open", estimated_value=10),
            Opportunity(company_id=other.id, contact_id=other_contact.id, name="Other", status="open", estimated_value=999999),
        ])
        db.session.commit()
        _login(client, user.id)
        html = client.get(f"/contacts?search=Alpha&tag=vip&stage=lead&page=1&company_id={other.id}").get_data(as_text=True)
        assert "alpha@example.com" in html
        assert "alpha-other@example.com" not in html
        assert "999999" not in html
        assert "arch@example.com" not in html
        archived_html = client.get("/contacts?archived=1&search=Archived").get_data(as_text=True)
        assert "arch@example.com" in archived_html


def test_contacts_missing_optional_opportunities_still_returns_200(audience_app):
    client = audience_app.test_client()
    with audience_app.app_context():
        user, company = _tenant_user()
        contact = Contact(company_id=company.id, email="opp@example.com", first_name="Opp", is_active=True)
        db.session.add(contact)
        db.session.flush()
        db.session.add(Opportunity(company_id=company.id, contact_id=contact.id, name="Open", status="closed", estimated_value=100))
        db.session.commit()
        _login(client, user.id)
        assert client.get("/contacts").status_code == 200


def test_contacts_unexpected_db_exception_rolls_back_and_correlates(audience_app, monkeypatch):
    client = audience_app.test_client()
    with audience_app.app_context():
        user, _company = _tenant_user()
        _login(client, user.id)
        called = {"rollback": False}
        monkeypatch.setattr(db.session, "rollback", lambda: called.__setitem__("rollback", True))
        monkeypatch.setattr(Contact.query.__class__, "paginate", lambda *a, **k: (_ for _ in ()).throw(SQLAlchemyError("boom")), raising=False)
        resp = client.get("/contacts", headers={"X-Request-ID": "rid-contacts"})
        assert resp.status_code == 500
        body = resp.get_data(as_text=True)
        assert "rid-contacts" in body
        assert "boom" not in body
        assert called["rollback"] is True


def test_contacts_bounds_page_and_filter_inputs(audience_app, monkeypatch):
    client = audience_app.test_client()
    with audience_app.app_context():
        user, _company = _tenant_user()
        _login(client, user.id)
        observed = {}
        paginate = Contact.query.__class__.paginate

        def capture(query, *, page, per_page, error_out):
            observed.update(page=page, per_page=per_page)
            return paginate(query, page=page, per_page=per_page, error_out=error_out)

        monkeypatch.setattr(Contact.query.__class__, "paginate", capture)
        assert client.get("/contacts?page=999999999&search=" + ("x" * 1000)).status_code == 200
        assert observed == {"page": 10000, "per_page": 20}
