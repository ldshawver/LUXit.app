from werkzeug.security import generate_password_hash
import pytest

from app import create_app
from extensions import db
from models import Company, IntegrationAuditLog, PhoneNumberUserPermission, PushSubscription, TwilioPhoneNumber, User, UserCompanyAccess
from services.user_lifecycle import active_owner_user_ids, active_team_member_count, archive_user_for_company, restore_user_for_company


@pytest.fixture
def app():
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
    yield app


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def _user(name, company=None, *, active=True, is_admin=False):
    u = User(username=name, email=f"{name}@users.test", password_hash=generate_password_hash("pw"), active=active, is_admin=is_admin)
    if company:
        u.default_company_id = company.id
    db.session.add(u); db.session.flush()
    return u


def _company(name="User Archive Co", seats=None):
    c = Company(name=name, is_active=True, max_team_members=seats)
    db.session.add(c); db.session.flush()
    return c


def _access(user, company, role="viewer", active=True, **kw):
    acc = UserCompanyAccess(user_id=user.id, company_id=company.id, role=role, is_active=active, **kw)
    db.session.add(acc); db.session.flush()
    return acc


def _login(client, user_or_id):
    uid = getattr(user_or_id, "id", user_or_id)
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True


def test_final_owner_detection_counts_direct_and_access_paths_distinctly(app):
    with app.app_context():
        co = _company(); other = _company("Other")
        direct_owner = _user("direct_owner", co, is_admin=True)
        access_owner = _user("access_owner")
        _access(access_owner, co, role="owner")
        both = _user("both_owner", co, is_admin=True)
        _access(both, co, role="owner")
        inactive = _user("inactive_owner", co, active=False, is_admin=True)
        other_owner = _user("other_owner", other, is_admin=True)
        db.session.commit()
        assert active_owner_user_ids(co.id) == {direct_owner.id, access_owner.id, both.id}
        assert inactive.id not in active_owner_user_ids(co.id)
        assert other_owner.id not in active_owner_user_ids(co.id)


def test_final_owner_archive_blocks_only_owner_and_allows_other_membership_paths(app):
    with app.app_context():
        co = _company(); actor = _user("actor", co)
        target = _user("target")
        _access(actor, co, role="staff")
        _access(target, co, role="owner")
        db.session.commit()
        with pytest.raises(ValueError, match="final active owner"):
            archive_user_for_company(target, co.id, actor)
        other_owner = _user("other_direct", co, is_admin=True)
        db.session.flush()
        archive_user_for_company(target, co.id, actor)
        db.session.commit()
        assert target.active is False
        assert other_owner.id in active_owner_user_ids(co.id)


def test_seat_counts_distinct_active_users_and_restore_no_double_count(app):
    with app.app_context():
        co = _company(seats=4); other = _company("Other Seats")
        direct = _user("seat_direct", co)
        access = _user("seat_access"); _access(access, co)
        both = _user("seat_both", co); _access(both, co)
        inactive = _user("seat_inactive", co, active=False); _access(inactive, co)
        outsider = _user("seat_out", other); _access(outsider, other)
        db.session.commit()
        assert active_team_member_count(co.id) == 3
        assert co.team_member_count == 3
        assert co.team_seats_available == 1
        actor = _user("seat_actor", co, is_admin=True); _access(actor, co, role="owner")
        db.session.commit()
        archive_user_for_company(access, co.id, actor); db.session.commit()
        assert active_team_member_count(co.id) == 3  # direct, both, actor
        restore_user_for_company(access, co.id, actor); db.session.commit()
        assert active_team_member_count(co.id) == 4


def test_archive_restore_route_revokes_access_denies_old_session_and_keeps_history(client, app):
    with app.app_context():
        co = _company(); owner = _user("route_owner", co); _access(owner, co, role="owner", manage_users_enabled=True)
        staff = _user("route_staff", co); _access(staff, co, role="staff", pwa_access_enabled=True, can_access_mobile_inbox=True)
        pn = TwilioPhoneNumber(company_id=co.id, phone_number="+15551234567", is_active=True)
        db.session.add(pn); db.session.flush()
        db.session.add(PhoneNumberUserPermission(company_id=co.id, phone_number_id=pn.id, user_id=staff.id, can_access_pwa=True))
        db.session.add(PushSubscription(company_id=co.id, user_id=staff.id, endpoint="https://push.users.test/1", is_active=True))
        db.session.commit(); owner_id, staff_id = owner.id, staff.id
    staff_client = app.test_client()
    login_resp = staff_client.post("/auth/login", data={"username":"route_staff", "password":"pw"}, follow_redirects=False)
    assert login_resp.status_code in {302, 303}
    with app.app_context():
        owner = db.session.get(User, owner_id)
        staff = db.session.get(User, staff_id)
        archive_user_for_company(staff, co.id, owner)
        db.session.commit()
        assert staff.active is False and staff.archived_at is not None
        assert UserCompanyAccess.query.filter_by(user_id=staff_id, company_id=co.id, is_active=False).first()
        assert PushSubscription.query.filter_by(user_id=staff_id, company_id=co.id, is_active=True).count() == 0
        assert IntegrationAuditLog.query.filter_by(company_id=co.id, service_slug="users", action="archive").count() == 1
    denied = staff_client.get("/dashboard", follow_redirects=False)
    assert denied.status_code in {302, 401}
    with app.app_context():
        owner = db.session.get(User, owner_id)
        staff = db.session.get(User, staff_id)
        restore_user_for_company(staff, co.id, owner)
        db.session.commit()
    assert staff_client.get("/dashboard", follow_redirects=False).status_code in {302, 401}
    with app.app_context():
        staff = db.session.get(User, staff_id)
        assert staff.active is True and staff.default_company_id == co.id
        assert UserCompanyAccess.query.filter_by(user_id=staff_id, company_id=co.id, is_active=True).first()
        assert IntegrationAuditLog.query.filter_by(company_id=co.id, service_slug="users", action="restore").count() == 1


def test_cross_tenant_and_global_admin_archive_protection(client, app):
    with app.app_context():
        co = _company("Tenant A"); other = _company("Tenant B")
        owner = _user("tenant_owner", co); _access(owner, co, role="owner", manage_users_enabled=True)
        outsider = _user("tenant_outsider", other); _access(outsider, other, role="staff")
        platform = _user("platform_admin", co, is_admin=True)
        db.session.commit(); owner_id, outsider_id, platform_id = owner.id, outsider.id, platform.id
    _login(client, owner_id)
    assert client.post(f"/user/delete/{outsider_id}", follow_redirects=True).status_code == 200
    with app.app_context():
        assert db.session.get(User, outsider_id).active is True
    assert client.post(f"/user/delete/{platform_id}", follow_redirects=True).status_code == 200
    with app.app_context():
        assert db.session.get(User, platform_id).active is True
