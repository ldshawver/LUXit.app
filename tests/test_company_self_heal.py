"""Tenant self-heal / default-company resolution — tenant-isolation regression.

These tests lock in the repair of a launch-blocking defect: the startup
self-heal and ``ensure_default_company_context`` used to attach *any* user to
the lowest-id active company (creating membership + granting owner/full_app).

Invariant under test: a user is only ever bound to a company they are *already*
authorized to access. No membership -> fail closed.
"""

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import Company, User, UserCompanyAccess, user_company
from tenant_self_heal import run_startup_self_heal


@pytest.fixture()
def app_ctx(monkeypatch):
    import scheduler

    monkeypatch.setattr(scheduler, "init_scheduler", lambda app: None)

    app = create_app()
    app.config.update(TESTING=True, SECRET_KEY="test-secret", SERVER_NAME="localhost")

    with app.app_context():
        db.create_all()
        _clear_company_state()
        yield app
        db.session.remove()
        db.drop_all()


def _clear_company_state():
    db.session.execute(user_company.delete())
    UserCompanyAccess.query.delete()
    User.query.delete()
    Company.query.delete()
    db.session.commit()


def _mk_company(name, is_active=True):
    c = Company(name=name, is_active=is_active)
    db.session.add(c)
    db.session.commit()
    return c


def _mk_user(username, is_admin=False, default_company_id=None):
    u = User(
        username=username,
        email=f"{username}@example.com",
        password_hash=generate_password_hash("secretpass"),
        is_admin=is_admin,
        default_company_id=default_company_id,
    )
    db.session.add(u)
    db.session.commit()
    return u


def _grant(user, company, role=UserCompanyAccess.ROLE_VIEWER, is_default=False):
    acc = UserCompanyAccess(
        user_id=user.id,
        company_id=company.id,
        role=role,
        is_default=is_default,
        is_active=True,
    )
    db.session.add(acc)
    db.session.commit()
    return acc


# ---------------------------------------------------------------------------
# TEST A — non-admin unbound user stays unbound
# ---------------------------------------------------------------------------
def test_a_non_admin_unbound_stays_unbound(app_ctx):
    a = _mk_company("Company A")
    b = _mk_company("Company B")
    x = _mk_user("plainx", is_admin=False)

    run_startup_self_heal()
    db.session.expire_all()

    assert UserCompanyAccess.query.filter_by(user_id=x.id).count() == 0
    assert db.session.get(User, x.id).default_company_id is None
    # Companies untouched
    assert db.session.get(Company, a.id).is_active is True
    assert db.session.get(Company, b.id).is_active is True
    assert Company.query.count() == 2
    assert x.get_default_company() is None


# ---------------------------------------------------------------------------
# TEST B — lowest-id trap: user belongs to B only, must resolve to B not A
# ---------------------------------------------------------------------------
def test_b_lowest_id_trap(app_ctx):
    a = _mk_company("Company A")  # lower id
    b = _mk_company("Company B")  # higher id
    assert a.id < b.id

    u = _mk_user("belongs_b", is_admin=False)
    _grant(u, b, role=UserCompanyAccess.ROLE_EDITOR)  # note: no is_default flag

    run_startup_self_heal()
    db.session.expire_all()

    resolved = db.session.get(User, u.id).get_default_company()
    assert resolved is not None
    assert resolved.id == b.id
    assert db.session.get(User, u.id).default_company_id == b.id
    # never gained access to A
    assert UserCompanyAccess.query.filter_by(user_id=u.id, company_id=a.id).count() == 0


def test_b_admin_lowest_id_trap(app_ctx):
    a = _mk_company("Company A")
    b = _mk_company("Company B")
    admin = _mk_user("admin_b", is_admin=True)
    _grant(admin, b, role=UserCompanyAccess.ROLE_OWNER)

    resolved = admin.ensure_default_company_context()
    assert resolved is not None and resolved.id == b.id
    assert admin.default_company_id == b.id
    assert UserCompanyAccess.query.filter_by(user_id=admin.id, company_id=a.id).count() == 0


# ---------------------------------------------------------------------------
# TEST C — healthy context is never mutated
# ---------------------------------------------------------------------------
def test_c_healthy_context_no_changes(app_ctx):
    a = _mk_company("Company A")
    u = _mk_user("healthy", is_admin=False, default_company_id=a.id)
    _grant(u, a, role=UserCompanyAccess.ROLE_MANAGER, is_default=True)

    for _ in range(3):
        result = run_startup_self_heal()
        assert result["repaired"] == 0

    db.session.expire_all()
    row = UserCompanyAccess.query.filter_by(user_id=u.id).one()
    assert row.role == UserCompanyAccess.ROLE_MANAGER
    assert row.is_default is True
    assert db.session.get(User, u.id).default_company_id == a.id
    assert UserCompanyAccess.query.filter_by(user_id=u.id).count() == 1


# ---------------------------------------------------------------------------
# TEST D — multiple authorized companies: pick only from the authorized set
# ---------------------------------------------------------------------------
def test_d_multiple_authorized_companies(app_ctx):
    a = _mk_company("Alpha")
    b = _mk_company("Bravo")
    c = _mk_company("Unrelated Charlie")
    u = _mk_user("multi", is_admin=False)
    _grant(u, a, role=UserCompanyAccess.ROLE_EDITOR)
    _grant(u, b, role=UserCompanyAccess.ROLE_EDITOR)

    run_startup_self_heal()
    db.session.expire_all()

    resolved = db.session.get(User, u.id).get_default_company()
    assert resolved is not None
    assert resolved.id in {a.id, b.id}
    assert resolved.id != c.id
    # deterministic policy: alphabetical first among authorized -> Alpha
    assert resolved.id == a.id
    assert UserCompanyAccess.query.filter_by(user_id=u.id, company_id=c.id).count() == 0


# ---------------------------------------------------------------------------
# TEST E — no companies at all: no crash, no fabrication
# ---------------------------------------------------------------------------
def test_e_no_companies(app_ctx):
    admin = _mk_user("lonely_admin", is_admin=True)
    plain = _mk_user("lonely_plain", is_admin=False)

    result = run_startup_self_heal()
    assert "error" not in result
    db.session.expire_all()

    assert Company.query.count() == 0
    assert UserCompanyAccess.query.count() == 0
    assert db.session.get(User, admin.id).default_company_id is None
    assert db.session.get(User, plain.id).default_company_id is None
    assert admin.get_default_company() is None
    assert plain.get_default_company() is None


# ---------------------------------------------------------------------------
# TEST F — cross-tenant safety: tenant-B user never gets tenant-A access
# ---------------------------------------------------------------------------
def test_f_cross_tenant_safety(app_ctx):
    a = _mk_company("Tenant A")
    b = _mk_company("Tenant B")
    ub = _mk_user("tenant_b_user", is_admin=False)
    _grant(ub, b, role=UserCompanyAccess.ROLE_EDITOR, is_default=True)

    # simulate several restarts
    for _ in range(3):
        run_startup_self_heal()

    db.session.expire_all()
    accesses = UserCompanyAccess.query.filter_by(user_id=ub.id).all()
    assert {acc.company_id for acc in accesses} == {b.id}
    assert db.session.get(User, ub.id).default_company_id == b.id
    assert ub.get_default_company().id == b.id


def test_f_stale_default_pointer_to_foreign_tenant_is_ignored(app_ctx):
    a = _mk_company("Tenant A")
    b = _mk_company("Tenant B")
    # user is a member of B, but default_company_id wrongly points at A
    u = _mk_user("stale_ptr", is_admin=False, default_company_id=a.id)
    _grant(u, b, role=UserCompanyAccess.ROLE_EDITOR)

    resolved = u.get_default_company()
    assert resolved is not None and resolved.id == b.id
    # self-heal repairs the pointer to an authorized company only
    run_startup_self_heal()
    db.session.expire_all()
    assert db.session.get(User, u.id).default_company_id == b.id
    assert UserCompanyAccess.query.filter_by(user_id=u.id, company_id=a.id).count() == 0


# ---------------------------------------------------------------------------
# TEST G — idempotency
# ---------------------------------------------------------------------------
def test_g_idempotency(app_ctx):
    a = _mk_company("Alpha")
    b = _mk_company("Bravo")
    u1 = _mk_user("needs_default", is_admin=False)
    _grant(u1, a, role=UserCompanyAccess.ROLE_EDITOR)  # no default flag, no ptr
    u2 = _mk_user("healthy2", is_admin=False, default_company_id=b.id)
    _grant(u2, b, role=UserCompanyAccess.ROLE_EDITOR, is_default=True)

    first = run_startup_self_heal()
    assert first["repaired"] >= 1  # u1 needed a default
    second = run_startup_self_heal()
    assert second["repaired"] == 0
    third = run_startup_self_heal()
    assert third["repaired"] == 0


# ---------------------------------------------------------------------------
# TEST H — platform admin with no membership is not made a tenant owner
# ---------------------------------------------------------------------------
def test_h_platform_admin_no_membership(app_ctx):
    a = _mk_company("Existing Tenant A")
    b = _mk_company("Existing Tenant B")
    admin = _mk_user("platform_admin", is_admin=True)

    run_startup_self_heal()
    resolved = admin.get_default_company()
    db.session.expire_all()

    assert resolved is None
    assert UserCompanyAccess.query.filter_by(user_id=admin.id).count() == 0
    assert db.session.get(User, admin.id).default_company_id is None
    # existing tenants untouched
    assert Company.query.count() == 2
    assert db.session.get(Company, a.id).is_active is True
    assert db.session.get(Company, b.id).is_active is True


def test_h_inactive_only_company_not_reactivated_for_admin(app_ctx):
    """Admin + a single inactive company, no membership -> no reactivation."""
    dead = _mk_company("Dormant Tenant", is_active=False)
    admin = _mk_user("orphan_admin", is_admin=True)

    resolved = admin.get_default_company()

    assert resolved is None
    assert db.session.get(Company, dead.id).is_active is False
    assert UserCompanyAccess.query.filter_by(user_id=admin.id).count() == 0
    assert admin.default_company_id is None


# ---------------------------------------------------------------------------
# Healthy admin membership is left entirely unchanged
# ---------------------------------------------------------------------------
def test_healthy_admin_membership_unchanged(app_ctx):
    a = _mk_company("Admin Home")
    admin = _mk_user("home_admin", is_admin=True, default_company_id=a.id)
    _grant(admin, a, role=UserCompanyAccess.ROLE_OWNER, is_default=True)

    before = admin.ensure_default_company_context()
    assert before is not None and before.id == a.id

    for _ in range(2):
        assert run_startup_self_heal()["repaired"] == 0

    db.session.expire_all()
    row = UserCompanyAccess.query.filter_by(user_id=admin.id).one()
    assert row.role == UserCompanyAccess.ROLE_OWNER
    assert row.is_default is True
    assert db.session.get(User, admin.id).default_company_id == a.id
