"""Contact export/import are tenant-scoped and need edit rights.

Regression: with no default company the export query ran unfiltered (every
tenant's contacts), and any member — viewer included — could export or import.
"""
import io
import uuid

import pytest

from app import create_app
from extensions import db
from models import Company, Contact, User, UserCompanyAccess


@pytest.fixture
def env():
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    with app.app_context():
        tag = uuid.uuid4().hex[:10]
        a, b = Company(name=f"Export A {tag}"), Company(name=f"Export B {tag}")
        db.session.add_all([a, b])
        db.session.flush()
        db.session.add_all([
            Contact(company_id=a.id, email=f"mine-{tag}@a.example", first_name="Mine"),
            Contact(company_id=b.id, email=f"theirs-{tag}@b.example", first_name="Theirs"),
        ])
        users = {}
        for key, company, role in (("editor", a, "editor"), ("viewer", a, "viewer"), ("homeless", None, None)):
            u = User(username=f"exp-{key}-{tag}", email=f"exp-{key}-{tag}@example.com", password_hash="x",
                     default_company_id=company.id if company else None)
            db.session.add(u)
            db.session.flush()
            if company:
                db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            users[key] = u.id
        db.session.commit()
        yield app, tag, users


def _as(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def test_editor_exports_only_own_tenant(env):
    app, tag, users = env
    r = _as(app, users["editor"]).get("/contacts/export")
    assert r.status_code == 200
    assert f"mine-{tag}".encode() in r.data and f"theirs-{tag}".encode() not in r.data


@pytest.mark.parametrize("who", ["viewer", "homeless"])
def test_viewer_and_companyless_user_get_no_export(env, who):
    app, tag, users = env
    r = _as(app, users[who]).get("/contacts/export")
    assert r.status_code == 302
    assert b"@a.example" not in r.data and b"@b.example" not in r.data


@pytest.mark.parametrize("who", ["viewer", "homeless"])
def test_viewer_and_companyless_user_cannot_import(env, who):
    app, tag, users = env
    csv_bytes = f"email,first_name\nnew-{tag}@x.example,New\n".encode()
    r = _as(app, users[who]).post("/contacts/import", data={"file": (io.BytesIO(csv_bytes), "c.csv")},
                                  content_type="multipart/form-data")
    assert r.status_code == 302
    assert Contact.query.filter_by(email=f"new-{tag}@x.example").count() == 0
