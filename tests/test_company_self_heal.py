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


def test_admin_with_no_membership_stays_unbound(app_ctx):
    """An admin with no authoritative company membership must NOT be bound to
    a fabricated fallback company. Tenant membership is never invented."""
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

    assert company is None
    assert admin.default_company_id is None
    assert Company.query.count() == 0
    assert UserCompanyAccess.query.filter_by(user_id=admin.id).count() == 0


def test_admin_not_bound_to_unrelated_inactive_company(app_ctx):
    """An unrelated inactive company must never be reactivated and handed to an
    admin who has no membership in it (the 'only company' / lowest-id trap)."""
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

    assert resolved is None
    assert admin.default_company_id is None
    refreshed = db.session.get(Company, company.id)
    assert refreshed.is_active is False
    assert Company.query.count() == 1
    assert UserCompanyAccess.query.filter_by(user_id=admin.id).count() == 0
