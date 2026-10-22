"""PostgreSQL-only proofs for the canonical contact-edit service.

Runs against the real PostgreSQL instance used by the other *_postgres.py
suites (TEST_DATABASE_URL / lux_identity_hardening_test on port 5433) so
uniqueness/conflict behavior is proven against the actual production
database engine rather than SQLite.
"""
from __future__ import annotations

import os

import pytest

TEST_DB_NAME = "lux_identity_hardening_test"
TEST_DB_PORT = 5433


@pytest.fixture(scope="module")
def pg_app():
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL-only contact profile tests")
    from app import create_app
    from extensions import db

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        assert db.engine.url.database == TEST_DB_NAME
        assert db.engine.url.port == TEST_DB_PORT
    return app


@pytest.fixture(autouse=True)
def clean_pg(pg_app):
    from extensions import db
    with pg_app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()


def _company(name="PG Tenant"):
    from extensions import db
    from models import Company
    row = Company(name=name)
    db.session.add(row)
    db.session.flush()
    return row


def _contact(company, **kw):
    from extensions import db
    from models import Contact
    kw.setdefault("phone", "+12025550200")
    kw.setdefault("normalized_phone", kw["phone"])
    row = Contact(company_id=company.id, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def test_same_tenant_phone_conflict_rejected_on_postgres(pg_app):
    from extensions import db
    from services.contact_profile import ContactConflictError, update_contact_fields
    with pg_app.app_context():
        co = _company()
        _contact(co, first_name="Existing", phone="+12025550201")
        target = _contact(co, first_name="Target", phone="+12025550202")
        with pytest.raises(ContactConflictError) as exc:
            update_contact_fields(target, company_id=co.id, fields={"phone": "+12025550201"}, source="manual")
        assert exc.value.field == "phone"


def test_same_tenant_email_conflict_rejected_on_postgres(pg_app):
    from extensions import db
    from services.contact_profile import ContactConflictError, update_contact_fields
    with pg_app.app_context():
        co = _company()
        _contact(co, first_name="Existing", email="taken@example.com", normalized_email="taken@example.com", phone="+12025550203")
        target = _contact(co, first_name="Target", phone="+12025550204")
        with pytest.raises(ContactConflictError) as exc:
            update_contact_fields(target, company_id=co.id, fields={"email": "taken@example.com"}, source="manual")
        assert exc.value.field == "email"


def test_cross_tenant_same_phone_isolated_on_postgres(pg_app):
    from extensions import db
    from services.contact_profile import update_contact_fields
    with pg_app.app_context():
        a = _company("A")
        b = _company("B")
        _contact(a, first_name="A-side", phone="+12025550205")
        target = _contact(b, first_name="B-side", phone="+12025550206")
        update_contact_fields(target, company_id=b.id, fields={"phone": "+12025550205"}, source="manual")
        assert target.normalized_phone == "+12025550205"
        # Tenant A's contact is untouched and still owns the number in its tenant.
        a_contact = db.session.query(type(target)).filter_by(company_id=a.id, normalized_phone="+12025550205").first()
        assert a_contact is not None
        assert a_contact.first_name == "A-side"


def test_sequential_conflicting_updates_second_one_rejected(pg_app):
    """Two updates racing for the same number: the first commits, and the
    second -- even issued immediately after -- must see the committed row
    and be rejected rather than creating a duplicate phone owner.
    """
    from extensions import db
    from services.contact_profile import ContactConflictError, update_contact_fields
    with pg_app.app_context():
        co = _company()
        holder = _contact(co, first_name="Holder", phone="+12025550207")
        challenger = _contact(co, first_name="Challenger", phone="+12025550208")

        update_contact_fields(holder, company_id=co.id, fields={"phone": "+12025550299"}, source="manual")
        db.session.commit()

        with pytest.raises(ContactConflictError):
            update_contact_fields(challenger, company_id=co.id, fields={"phone": "+12025550299"}, source="manual")
        db.session.rollback()

        from models import Contact
        assert Contact.query.filter_by(company_id=co.id, normalized_phone="+12025550299").count() == 1
