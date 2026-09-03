"""Regression coverage for the startup tenant-context self-heal.

Historical defect: the startup self-heal (app.py) and
``User.ensure_default_company_context`` (models.py) would attach an unbound
user to whichever company happened to have the lowest active id
(``Company.query.filter_by(is_active=True).order_by(Company.id.asc()).first()``),
creating a ``UserCompanyAccess`` row with ``is_default`` / ``can_access_full_app``
and setting ``default_company_id``. In a multi-tenant deployment this
cross-binds unrelated tenants.

Invariant under test: tenant membership is only ever taken from authoritative
state (the user's own ``UserCompanyAccess`` rows / legacy link / an explicitly
provisioned default). If the user has none, the self-heal fails closed.
"""

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
        db.session.execute(user_company.delete())
        UserCompanyAccess.query.delete()
        User.query.delete()
        Company.query.delete()
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


def _run_startup_self_heal():
    """Replicate the app.py startup reconciliation loop exactly."""
    changed = 0
    for user in User.query.all():
        before = user.default_company_id
        user.ensure_default_company_context()
        if user.default_company_id != before:
            changed += 1
    return changed


def _mk_user(username, **kw):
    u = User(
        username=username,
        email=f"{username}@example.com",
        password_hash=generate_password_hash("secretpass"),
        **kw,
    )
    db.session.add(u)
    db.session.flush()
    return u


def _mk_company(name, is_active=True):
    c = Company(name=name, is_active=is_active)
    db.session.add(c)
    db.session.flush()
    return c


# ── TEST A — non-admin unbound stays unbound ────────────────────────────────
def test_a_non_admin_unbound_stays_unbound(app_ctx):
    company_a = _mk_company("Company A")
    company_b = _mk_company("Company B")
    user_x = _mk_user("user_x", is_admin=False)
    db.session.commit()

    _run_startup_self_heal()

    assert UserCompanyAccess.query.filter_by(user_id=user_x.id).count() == 0
    assert db.session.get(User, user_x.id).default_company_id is None
    for c in (company_a, company_b):
        refreshed = db.session.get(Company, c.id)
        assert refreshed.is_active is True
    assert Company.query.count() == 2


# ── TEST B — lowest-id trap ─────────────────────────────────────────────────
def test_b_lowest_id_trap(app_ctx):
    company_a = _mk_company("Company A")  # lower id
    company_b = _mk_company("Company B")  # higher id
    assert company_a.id < company_b.id
    user = _mk_user("only_b", is_admin=False)
    db.session.add(
        UserCompanyAccess(user_id=user.id, company_id=company_b.id, role="viewer")
    )
    db.session.commit()

    resolved = user.ensure_default_company_context()

    assert resolved is not None
    assert resolved.id == company_b.id
    assert db.session.get(User, user.id).default_company_id == company_b.id
    assert (
        UserCompanyAccess.query.filter_by(
            user_id=user.id, company_id=company_a.id
        ).count()
        == 0
    )


# ── TEST C — healthy context is untouched ───────────────────────────────────
def test_c_healthy_context_no_change(app_ctx):
    company_a = _mk_company("Company A")
    user = _mk_user("healthy", is_admin=False, default_company_id=company_a.id)
    db.session.add(
        UserCompanyAccess(
            user_id=user.id,
            company_id=company_a.id,
            role="viewer",
            is_default=True,
        )
    )
    db.session.commit()

    for _ in range(3):
        assert _run_startup_self_heal() == 0

    assert db.session.get(User, user.id).default_company_id == company_a.id
    assert UserCompanyAccess.query.filter_by(user_id=user.id).count() == 1


# ── TEST D — multiple authorized companies use documented policy ────────────
def test_d_multiple_authorized_companies(app_ctx):
    company_c = _mk_company("Company C")  # unrelated
    company_a = _mk_company("Company A")
    company_b = _mk_company("Company B")
    user = _mk_user("multi", is_admin=False)
    db.session.add_all(
        [
            UserCompanyAccess(user_id=user.id, company_id=company_a.id, role="viewer"),
            UserCompanyAccess(user_id=user.id, company_id=company_b.id, role="viewer"),
        ]
    )
    db.session.commit()

    resolved = user.ensure_default_company_context()

    # get_all_companies() orders by name -> "Company A" wins; never Company C.
    assert resolved.id in {company_a.id, company_b.id}
    assert resolved.id == company_a.id
    assert db.session.get(User, user.id).default_company_id != company_c.id


# ── TEST E — no companies at all ────────────────────────────────────────────
def test_e_no_companies(app_ctx):
    user = _mk_user("lonely", is_admin=False)
    admin = _mk_user("lonely_admin", is_admin=True)
    db.session.commit()

    assert _run_startup_self_heal() == 0

    assert Company.query.count() == 0
    assert UserCompanyAccess.query.count() == 0
    assert db.session.get(User, user.id).default_company_id is None
    assert db.session.get(User, admin.id).default_company_id is None


# ── TEST F — cross-tenant safety after restart ──────────────────────────────
def test_f_cross_tenant_safety(app_ctx):
    tenant_a = _mk_company("Tenant A")
    tenant_b = _mk_company("Tenant B")
    user_b = _mk_user("belongs_b", is_admin=False, default_company_id=tenant_b.id)
    db.session.add(
        UserCompanyAccess(
            user_id=user_b.id, company_id=tenant_b.id, role="viewer", is_default=True
        )
    )
    db.session.commit()

    for _ in range(2):
        _run_startup_self_heal()

    assert (
        UserCompanyAccess.query.filter_by(
            user_id=user_b.id, company_id=tenant_a.id
        ).count()
        == 0
    )
    assert db.session.get(User, user_b.id).default_company_id == tenant_b.id
    legacy = db.session.execute(
        user_company.select().where(
            (user_company.c.user_id == user_b.id)
            & (user_company.c.company_id == tenant_a.id)
        )
    ).first()
    assert legacy is None


# ── TEST G — idempotency ───────────────────────────────────────────────────
def test_g_idempotency(app_ctx):
    company = _mk_company("Company A")
    user = _mk_user("idem", is_admin=True, default_company_id=company.id)
    db.session.commit()

    first = _run_startup_self_heal()
    uca_after_first = UserCompanyAccess.query.filter_by(user_id=user.id).count()

    second = _run_startup_self_heal()
    uca_after_second = UserCompanyAccess.query.filter_by(user_id=user.id).count()

    assert second == 0
    assert uca_after_first == uca_after_second


# ── TEST I — archived user (inactive access row) is not resurrected ─────────
def test_i_archived_user_not_resurrected(app_ctx):
    company = _mk_company("Company A")
    user = _mk_user("archived", is_admin=False, default_company_id=company.id)
    db.session.add(
        UserCompanyAccess(
            user_id=user.id,
            company_id=company.id,
            role="staff",
            is_default=True,
            is_active=False,  # access was revoked
        )
    )
    db.session.commit()

    resolved = user.ensure_default_company_context()

    assert resolved is None
    # No legacy user_company link fabricated, access row still inactive.
    legacy = db.session.execute(
        user_company.select().where(user_company.c.user_id == user.id)
    ).first()
    assert legacy is None
    row = UserCompanyAccess.query.filter_by(user_id=user.id).one()
    assert row.is_active is False


# ── TEST H — platform admin never fabricates tenant membership ──────────────
def test_h_platform_admin_no_fabricated_membership(app_ctx):
    tenant_a = _mk_company("Tenant A")
    tenant_b = _mk_company("Tenant B")
    platform_admin = _mk_user("platform", is_admin=True)
    db.session.commit()

    _run_startup_self_heal()
    resolved = platform_admin.get_default_company()

    assert resolved is None
    assert db.session.get(User, platform_admin.id).default_company_id is None
    assert UserCompanyAccess.query.filter_by(user_id=platform_admin.id).count() == 0
    for c in (tenant_a, tenant_b):
        assert db.session.get(Company, c.id).is_active is True
