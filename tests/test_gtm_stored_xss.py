"""Tenant-authored HTML must not execute as page script on the app origin.

(EmailTemplate has no content column at all, so the template-preview sandbox
change has nothing to store yet and is not tested here.)
"""
from datetime import datetime

import pytest

from app import create_app
from extensions import db
from models import Company, NewsletterArchive, User, UserCompanyAccess

PAYLOAD = '<img src=x onerror="alert(1)"><script>alert(2)</script>`${alert(3)}`'


@pytest.fixture
def signed_in():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        co = Company(name="XSS Co")
        db.session.add(co)
        db.session.flush()
        u = User(username="xss-viewer", email="xss@example.com", default_company_id=co.id, password_hash="x")
        db.session.add(u)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role="owner", is_default=True))
        db.session.add(NewsletterArchive(title="Public", slug="public-1", html_content=PAYLOAD, is_public=True, published_at=datetime.utcnow()))
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True
        yield app, client
        db.session.remove()
        db.drop_all()


def test_public_newsletter_archive_strips_markup(signed_in):
    app, _ = signed_in
    resp = app.test_client().get("/newsletter-archive")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Public" in body
    assert "<script>alert(2)</script>" not in body
    assert "onerror=" not in body
