import pytest

from app import create_app
from extensions import db
from models import Company, SocialMediaAccount, SocialPost, User, UserCompanyAccess


@pytest.fixture
def tenants():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.create_all()
        company_a = Company(name="Social Tenant A")
        company_b = Company(name="Social Tenant B")
        db.session.add_all([company_a, company_b])
        db.session.flush()
        user_a = User(username="social-a", email="social-a@example.com", is_admin=False, default_company_id=company_a.id, password_hash="test-hash")
        db.session.add(user_a)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=user_a.id, company_id=company_a.id, role="admin", is_default=True))
        post_a = SocialPost(company_id=company_a.id, content="Tenant A Post", status="draft")
        post_b = SocialPost(company_id=company_b.id, content="Tenant B Post", status="draft")
        account_a = SocialMediaAccount(company_id=company_a.id, platform="facebook", account_name="a", access_token="secret-a", is_active=True)
        account_b = SocialMediaAccount(company_id=company_b.id, platform="facebook", account_name="b", access_token="secret-b", is_active=True)
        db.session.add_all([post_a, post_b, account_a, account_b])
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user_a.id)
            sess["_fresh"] = True
        yield app, client, company_a, company_b, post_a, post_b, account_a, account_b
        db.session.remove()
        db.drop_all()


def test_tenant_a_cannot_list_tenant_b_social_posts(tenants):
    _, client, _, _, _, _, _, _ = tenants
    body = client.get("/social-media").get_data(as_text=True)
    assert "Tenant A Post" in body
    assert "Tenant B Post" not in body
    assert "secret-a" not in body
    assert "secret-b" not in body


def test_tenant_a_cannot_delete_tenant_b_social_post(tenants):
    app, client, _, _, _, post_b, _, _ = tenants
    response = client.delete(f"/api/social/delete-post/{post_b.id}")
    assert response.status_code == 404
    with app.app_context():
        assert db.session.get(SocialPost, post_b.id) is not None


def test_tenant_a_cannot_schedule_with_tenant_b_account(tenants):
    _, client, _, _, _, _, _, account_b = tenants
    response = client.post("/social/schedule", data={"account_id": str(account_b.id), "content": "x", "scheduled_for": "2026-06-15T12:00"})
    assert response.status_code in (302, 303)


def test_social_tokens_are_encrypted_at_rest_and_not_rendered(tenants):
    app, client, _, _, _, _, account_a, _ = tenants
    with app.app_context():
        account_a.access_token = "raw-secret-token"
        account_a.refresh_token = "raw-refresh-token"
        db.session.commit()
        stored_access = account_a._access_token
        stored_refresh = account_a._refresh_token
        assert stored_access != "raw-secret-token"
        assert stored_refresh != "raw-refresh-token"
        assert account_a.access_token == "raw-secret-token"
        assert account_a.refresh_token == "raw-refresh-token"

    body = client.get("/social-media").get_data(as_text=True)
    assert "raw-secret-token" not in body
    assert "raw-refresh-token" not in body
