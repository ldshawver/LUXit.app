import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import Company, User, UserCompanyAccess, user_company


@pytest.fixture()
def app_ctx(monkeypatch):
    import scheduler

    monkeypatch.setattr(scheduler, "init_scheduler", lambda app: None)

    app = create_app()
    app.config.update(TESTING=True, SECRET_KEY="test-secret", SERVER_NAME="localhost")

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _clear_company_state():
    db.session.execute(user_company.delete())
    UserCompanyAccess.query.delete()
    Company.query.delete()
    db.session.commit()


def test_admin_get_default_company_creates_fallback_company(app_ctx):
    _clear_company_state()
    admin = User(
        username="luke",
        email="luke@adiken.com",
        password_hash=generate_password_hash("secretpass"),
        is_admin=True,
    )
    db.session.add(admin)
    db.session.commit()

    company = admin.get_default_company()

    assert company is not None
    assert company.name == "LUXit Marketing"
    assert company.is_active is True
    assert admin.default_company_id == company.id

    access = UserCompanyAccess.query.filter_by(
        user_id=admin.id, company_id=company.id
    ).one()
    assert access.role == UserCompanyAccess.ROLE_OWNER
    assert access.is_default is True
    assert access.can_access_full_app is True
    assert access.can_access_mobile_inbox is True


def test_admin_get_default_company_reactivates_inactive_company(app_ctx):
    _clear_company_state()
    company = Company(name="Existing Tenant", is_active=False)
    admin = User(
        username="admin",
        email="admin@example.com",
        password_hash=generate_password_hash("secretpass"),
        is_admin=True,
    )
    db.session.add_all([company, admin])
    db.session.commit()

    resolved = admin.get_default_company()

    assert resolved.id == company.id
    assert resolved.is_active is True
    assert admin.default_company_id == company.id
    assert Company.query.count() == 1
