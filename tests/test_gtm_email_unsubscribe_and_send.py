"""Email opt-out mechanism (CAN-SPAM) and honest campaign-send controls."""
import pytest

from app import create_app
from extensions import db
from models import Campaign, Company, Contact, MarketingAuditLog, User, UserCompanyAccess
from services.contact_consent import has_explicit_email_opt_out


@pytest.fixture
def env():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=True, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        a = Company(name="Unsub A")
        b = Company(name="Unsub B")
        db.session.add_all([a, b])
        db.session.flush()
        contact = Contact(company_id=a.id, email="reader@a.example", is_active=True)
        other = Contact(company_id=b.id, email="other@b.example", is_active=True)
        owner = User(username="unsub-owner", email="unsub-owner@example.com", default_company_id=a.id, password_hash="x")
        db.session.add_all([contact, other, owner])
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=owner.id, company_id=a.id, role="owner", is_default=True))
        camp = Campaign(company_id=a.id, name="Fall promo", subject="Hi", status="draft")
        db.session.add(camp)
        db.session.commit()
        yield app, contact.id, other.id, owner.id, camp.id
        db.session.remove()
        db.drop_all()


def _url(app, contact_id):
    from services.email_unsubscribe import unsubscribe_url
    with app.test_request_context():
        return unsubscribe_url(db.session.get(Contact, contact_id))


def test_render_uses_real_unsubscribe_link_not_placeholder(env):
    app, contact_id, *_ = env
    from email_service import EmailService
    with app.test_request_context():
        contact = db.session.get(Contact, contact_id)
        html = EmailService().render_template("<a href='{{ unsubscribe_url }}'>u</a>", contact, Campaign(id=1, name="n", subject="s"))
    assert "yourdomain.com" not in html
    assert "/email/unsubscribe/" in html
    assert "reader@a.example" not in html


def test_unsubscribe_link_confirms_then_opts_out(env):
    app, contact_id, *_ = env
    path = _url(app, contact_id).split("luxit.app", 1)[1]
    client = app.test_client()
    page = client.get(path)
    assert page.status_code == 200 and "Unsubscribe" in page.get_data(as_text=True)
    with app.app_context():
        assert not has_explicit_email_opt_out(db.session.get(Contact, contact_id))
    done = client.post(path)
    assert done.status_code == 200 and "unsubscribed" in done.get_data(as_text=True)
    with app.app_context():
        c = db.session.get(Contact, contact_id)
        assert has_explicit_email_opt_out(c)
        assert MarketingAuditLog.query.filter_by(entity_id=contact_id, action="email_unsubscribed_via_link").count() == 1


def test_one_click_unsubscribe_works_without_csrf_token(env):
    app, contact_id, *_ = env
    path = _url(app, contact_id).split("luxit.app", 1)[1]
    resp = app.test_client().post(path, data={"List-Unsubscribe": "One-Click"})
    assert resp.status_code == 200
    with app.app_context():
        assert has_explicit_email_opt_out(db.session.get(Contact, contact_id))


def test_tampered_or_cross_tenant_token_is_rejected(env):
    app, contact_id, other_id, *_ = env
    path = _url(app, contact_id).split("luxit.app", 1)[1]
    client = app.test_client()
    assert client.post(path[:-3] + "xyz").status_code == 404
    # Token signed for contact A, but the row now belongs to another tenant ->
    # refuse. (Same session the request will use: the fixture's app context is
    # active, so the test client reuses it.)
    c = db.session.get(Contact, contact_id)
    c.company_id = db.session.get(Contact, other_id).company_id
    db.session.commit()
    assert client.post(path).status_code == 404


def test_repeat_unsubscribe_is_idempotent(env):
    app, contact_id, *_ = env
    path = _url(app, contact_id).split("luxit.app", 1)[1]
    client = app.test_client()
    client.post(path)
    client.post(path)
    with app.app_context():
        assert MarketingAuditLog.query.filter_by(entity_id=contact_id, action="email_unsubscribed_via_link").count() == 1


def test_send_button_does_not_claim_a_send_that_never_happens(env):
    app, _, _, owner_id, camp_id = env
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(owner_id)
        sess["_fresh"] = True
    body = client.post(f"/campaigns/{camp_id}/send", follow_redirects=True).get_data(as_text=True)
    assert "being sent" not in body
    assert "nothing was sent" in body
    with app.app_context():
        assert db.session.get(Campaign, camp_id).status == "draft"
