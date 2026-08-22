"""Tests for services/contact_profile.py — the canonical tenant-scoped
contact-edit service shared by the desktop CRM route and the PWA contact
panel — and the PWA PATCH endpoint that calls it.
"""
import os

import pytest

from app import create_app
from extensions import db
from models import (
    Company,
    Contact,
    ContactEmailAddress,
    ContactPhoneNumber,
    Segment,
    SegmentMember,
    TwilioAccount,
    TwilioConversation,
    User,
    UserCompanyAccess,
)
from services.contact_profile import (
    ContactConflictError,
    serialize_contact,
    update_contact_fields,
)


# ── Service-level tests (SQLite) ───────────────────────────────────────────

@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    a = create_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    with a.app_context():
        db.drop_all()
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


def make_company(name="Tenant"):
    c = Company(name=name)
    db.session.add(c)
    db.session.flush()
    return c


def make_contact(tenant, **kw):
    kw.setdefault("phone", "+12025550100")
    kw.setdefault("normalized_phone", kw["phone"])
    row = Contact(company_id=tenant.id, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def test_cross_tenant_update_denied(app):
    with app.app_context():
        a = make_company("A")
        b = make_company("B")
        contact = make_contact(a, first_name="Alice", phone="+12025550101")
        with pytest.raises(PermissionError):
            update_contact_fields(contact, company_id=b.id, fields={"first_name": "Mallory"}, source="manual")
        db.session.refresh(contact)
        assert contact.first_name == "Alice"


def test_cross_tenant_read_denied_via_scoped_query(app):
    with app.app_context():
        a = make_company("A")
        b = make_company("B")
        contact = make_contact(a, first_name="Alice", phone="+12025550102")
        # The canonical, tenant-scoped read pattern the service relies on.
        found = Contact.query.filter_by(id=contact.id, company_id=b.id).first()
        assert found is None


def test_same_tenant_authorized_edit_succeeds(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, first_name="Old", phone="+12025550103")
        updated = update_contact_fields(
            contact, company_id=co.id, fields={"first_name": "New"}, source="manual",
        )
        assert updated.first_name == "New"


def test_first_name_update(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, first_name="Old")
        update_contact_fields(contact, company_id=co.id, fields={"first_name": "Luke"}, source="manual")
        assert contact.first_name == "Luke"


def test_last_name_update(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, last_name="Old")
        update_contact_fields(contact, company_id=co.id, fields={"last_name": "Shawver"}, source="manual")
        assert contact.last_name == "Shawver"


def test_phone_normalization(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, phone="+12025550104")
        update_contact_fields(contact, company_id=co.id, fields={"phone": "(202) 555-0199"}, source="manual")
        assert contact.normalized_phone == "+12025550199"
        assert contact.primary_phone == "(202) 555-0199"


def test_email_normalization(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co)
        update_contact_fields(contact, company_id=co.id, fields={"email": "  Person@Example.COM  "}, source="manual")
        assert contact.normalized_email == "person@example.com"
        assert contact.email == "Person@Example.COM"


def test_malformed_phone_rejected(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, phone="+12025550105")
        with pytest.raises(ValueError):
            update_contact_fields(contact, company_id=co.id, fields={"phone": "not-a-phone"}, source="manual")
        db.session.refresh(contact)
        assert contact.normalized_phone == "+12025550105"


def test_malformed_email_rejected(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, email="old@example.com", normalized_phone="junk")
        with pytest.raises(ValueError):
            update_contact_fields(contact, company_id=co.id, fields={"email": "not-an-email"}, source="manual")
        db.session.refresh(contact)
        assert contact.email == "old@example.com"


def test_same_tenant_phone_conflict_rejected(app):
    with app.app_context():
        co = make_company()
        make_contact(co, first_name="Existing", phone="+12025550106")
        target = make_contact(co, first_name="Target", phone="+12025550107")
        with pytest.raises(ContactConflictError) as exc:
            update_contact_fields(target, company_id=co.id, fields={"phone": "+12025550106"}, source="manual")
        assert exc.value.field == "phone"
        db.session.refresh(target)
        assert target.normalized_phone == "+12025550107"


def test_same_tenant_email_conflict_rejected(app):
    with app.app_context():
        co = make_company()
        make_contact(co, first_name="Existing", email="taken@example.com", normalized_email="taken@example.com", phone="+12025550108")
        target = make_contact(co, first_name="Target", phone="+12025550109")
        with pytest.raises(ContactConflictError) as exc:
            update_contact_fields(target, company_id=co.id, fields={"email": "taken@example.com"}, source="manual")
        assert exc.value.field == "email"


def test_cross_tenant_same_phone_does_not_conflict(app):
    with app.app_context():
        a = make_company("A")
        b = make_company("B")
        make_contact(a, first_name="A-side", phone="+12025550110")
        target = make_contact(b, first_name="B-side", phone="+12025550111")
        update_contact_fields(target, company_id=b.id, fields={"phone": "+12025550110"}, source="manual")
        assert target.normalized_phone == "+12025550110"


def test_cross_tenant_same_email_does_not_leak(app):
    with app.app_context():
        a = make_company("A")
        b = make_company("B")
        make_contact(a, first_name="A-side", email="shared@example.com", normalized_email="shared@example.com", phone="+12025550112")
        target = make_contact(b, first_name="B-side", phone="+12025550113")
        # No exception, and nothing about tenant A is exposed by the call.
        update_contact_fields(target, company_id=b.id, fields={"email": "shared@example.com"}, source="manual")
        assert target.normalized_email == "shared@example.com"


def test_normalized_fields_update_atomically(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, phone="+12025550114")
        update_contact_fields(contact, company_id=co.id, fields={"phone": "202-555-0188"}, source="manual")
        assert contact.phone == contact.primary_phone == "202-555-0188"
        assert contact.normalized_phone == "+12025550188"


def test_contact_point_rows_remain_consistent(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, phone="+12025550115")
        update_contact_fields(contact, company_id=co.id, fields={"phone": "+12025550166", "email": "a@example.com"}, source="manual")
        phones = ContactPhoneNumber.query.filter_by(company_id=co.id, contact_id=contact.id).all()
        assert len(phones) == 1
        assert phones[0].normalized_value == "+12025550166"
        assert phones[0].is_primary is True

        emails = ContactEmailAddress.query.filter_by(company_id=co.id, contact_id=contact.id).all()
        assert len(emails) == 1
        assert emails[0].normalized_value == "a@example.com"
        assert emails[0].is_primary is True

        # Editing to a different number keeps the prior number's row as
        # (non-primary) history rather than deleting it -- exactly one row
        # is primary, and it matches the contact's current canonical phone.
        update_contact_fields(contact, company_id=co.id, fields={"phone": "+12025550177"}, source="manual")
        phones = ContactPhoneNumber.query.filter_by(company_id=co.id, contact_id=contact.id).all()
        assert len(phones) == 2
        primary = [p for p in phones if p.is_primary]
        assert len(primary) == 1
        assert primary[0].normalized_value == contact.normalized_phone == "+12025550177"

        # Editing back to a value that already has a row reuses it instead
        # of inserting a duplicate.
        update_contact_fields(contact, company_id=co.id, fields={"phone": "+12025550166"}, source="manual")
        phones = ContactPhoneNumber.query.filter_by(company_id=co.id, contact_id=contact.id).all()
        assert len(phones) == 2
        primary = [p for p in phones if p.is_primary]
        assert len(primary) == 1
        assert primary[0].normalized_value == "+12025550166"


def test_sms_consent_preserved(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, sms_marketing_opt_in=True, sms_consent_status="opted_in")
        update_contact_fields(contact, company_id=co.id, fields={"first_name": "New"}, source="manual")
        assert contact.sms_marketing_opt_in is True
        assert contact.sms_consent_status == "opted_in"


def test_email_consent_preserved(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, email_opt_in=True, email_consent_status="opted_in")
        update_contact_fields(contact, company_id=co.id, fields={"first_name": "New"}, source="manual")
        assert contact.email_opt_in is True
        assert contact.email_consent_status == "opted_in"


def test_stop_suppression_preserved(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, sms_opted_out=True, do_not_sms=True)
        update_contact_fields(contact, company_id=co.id, fields={"first_name": "New"}, source="manual")
        assert contact.sms_opted_out is True
        assert contact.do_not_sms is True


def test_tags_preserved_when_not_touched(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, tags="vip,repeat")
        update_contact_fields(contact, company_id=co.id, fields={"first_name": "New"}, source="manual")
        assert contact.tags == "vip,repeat"


def test_segment_memberships_preserved(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co)
        seg = Segment(company_id=co.id, name="VIPs")
        db.session.add(seg)
        db.session.flush()
        db.session.add(SegmentMember(segment_id=seg.id, contact_id=contact.id))
        db.session.flush()
        update_contact_fields(contact, company_id=co.id, fields={"first_name": "New"}, source="manual")
        assert SegmentMember.query.filter_by(contact_id=contact.id, segment_id=seg.id).count() == 1


def test_conversation_linkage_preserved(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co)
        conv = TwilioConversation(company_id=co.id, from_number=contact.phone, contact_id=contact.id)
        db.session.add(conv)
        db.session.flush()
        update_contact_fields(contact, company_id=co.id, fields={"first_name": "New"}, source="manual")
        db.session.refresh(conv)
        assert conv.contact_id == contact.id


def test_google_linkage_and_provenance_preserved(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(
            co, external_google_contact_id="people/c123",
            google_contact_resource_id="people/c123", google_match_status="matched",
        )
        update_contact_fields(contact, company_id=co.id, fields={"last_name": "New"}, source="manual")
        assert contact.external_google_contact_id == "people/c123"
        assert contact.google_contact_resource_id == "people/c123"
        assert contact.google_match_status == "matched"


def test_authorized_edit_reaches_minimum_established(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, phone=None, normalized_phone=None)
        assert contact.identity_status == "pending_identity"
        update_contact_fields(
            contact, company_id=co.id,
            fields={"first_name": "Luke", "phone": "+12025550120"}, source="pwa_verified",
        )
        assert contact.identity_status == "minimum_established"


def test_minimum_established_contact_skips_identity_collection(app):
    with app.app_context():
        from services.contact_identity import should_request_identity
        co = make_company()
        contact = make_contact(co, phone=None, normalized_phone=None)
        update_contact_fields(
            contact, company_id=co.id,
            fields={"first_name": "Luke", "phone": "+12025550121"}, source="pwa_verified",
        )
        assert contact.identity_status == "minimum_established"
        assert not should_request_identity(contact)


def test_serialize_contact_shape(app):
    with app.app_context():
        co = make_company()
        contact = make_contact(co, first_name="Luke", last_name="S", company="Acme", tags="vip")
        data = serialize_contact(contact)
        assert data["first_name"] == "Luke"
        assert data["full_name"] == "Luke S"
        assert data["company"] == "Acme"
        assert data["tags"] == "vip"


# ── PWA HTTP endpoint tests (reuses the `app` fixture above + a client) ─────

@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


def login(client, user_id):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


@pytest.fixture
def pwa_world(app):
    with app.app_context():
        co_a = Company(name="PWA Co A", is_active=True)
        co_b = Company(name="PWA Co B", is_active=True)
        db.session.add_all([co_a, co_b])
        db.session.flush()
        alice = User(username="pwa_alice", email="pwa_alice@test.com", password_hash="x", default_company_id=co_a.id)
        bob = User(username="pwa_bob", email="pwa_bob@test.com", password_hash="x", default_company_id=co_b.id)
        db.session.add_all([alice, bob])
        db.session.flush()
        db.session.add_all([
            UserCompanyAccess(user_id=alice.id, company_id=co_a.id, role=UserCompanyAccess.ROLE_ADMIN, is_default=True, can_access_mobile_inbox=True),
            UserCompanyAccess(user_id=bob.id, company_id=co_b.id, role=UserCompanyAccess.ROLE_ADMIN, is_default=True, can_access_mobile_inbox=True),
            TwilioAccount(company_id=co_a.id, from_phone="+15559998888", _account_sid="ACtest", _auth_token="auth"),
        ])
        db.session.flush()
        contact_a = Contact(company_id=co_a.id, first_name="Alice's", last_name="Contact", phone="+12025550130", normalized_phone="+12025550130")
        db.session.add(contact_a)
        db.session.flush()
        conv_a = TwilioConversation(company_id=co_a.id, from_number="+12025550130", to_number="+15559998888", contact_id=contact_a.id, contact_name="Alice's Contact")
        db.session.add(conv_a)
        db.session.commit()
        yield {"co_a": co_a.id, "co_b": co_b.id, "alice": alice.id, "bob": bob.id, "contact_a": contact_a.id, "conv_a": conv_a.id}


def test_pwa_api_cross_tenant_update_denied(client, app, pwa_world):
    login(client, pwa_world["bob"])
    res = client.patch(f"/api/inbox/conversations/{pwa_world['conv_a']}/contact", json={"first_name": "Mallory"})
    assert res.status_code == 404
    with app.app_context():
        c = db.session.get(Contact, pwa_world["contact_a"])
        assert c.first_name == "Alice's"


def test_pwa_api_returns_updated_canonical_json(client, app, pwa_world):
    login(client, pwa_world["alice"])
    res = client.patch(f"/api/inbox/conversations/{pwa_world['conv_a']}/contact", json={"first_name": "Renamed"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["contact"]["first_name"] == "Renamed"
    assert data["contact_name"] == "Renamed Contact"
    with app.app_context():
        c = db.session.get(Contact, pwa_world["contact_a"])
        assert c.first_name == "Renamed"


def test_pwa_api_conflict_response_does_not_leak_other_tenant(client, app, pwa_world):
    with app.app_context():
        # Another same-tenant contact already owns this phone number.
        db.session.add(Contact(company_id=pwa_world["co_a"], first_name="Other", phone="+12025550199", normalized_phone="+12025550199"))
        db.session.commit()

    login(client, pwa_world["alice"])
    res = client.patch(f"/api/inbox/conversations/{pwa_world['conv_a']}/contact", json={"phone": "+12025550199"})
    assert res.status_code == 409
    data = res.get_json()
    assert data["error"] == "contact_identity_conflict"
    assert data["field"] == "phone"
    # No contact id, name, or other tenant/contact identifying detail is exposed.
    assert "id" not in data
    assert "contact" not in data
    assert "Other" not in data["message"]


def test_pwa_api_malformed_phone_rejected(client, app, pwa_world):
    login(client, pwa_world["alice"])
    res = client.patch(f"/api/inbox/conversations/{pwa_world['conv_a']}/contact", json={"phone": "not-a-phone"})
    assert res.status_code == 400
    data = res.get_json()
    assert data["success"] is False
    assert data["error"] == "invalid_input"


# ── Desktop CRM HTTP endpoint tests ─────────────────────────────────────────

def test_desktop_crm_same_tenant_edit_succeeds(client, app, pwa_world):
    login(client, pwa_world["alice"])
    res = client.post(
        f"/api/contacts/{pwa_world['contact_a']}/update",
        data={"first_name": "DesktopEdited"},
    )
    assert res.status_code == 200
    with app.app_context():
        c = db.session.get(Contact, pwa_world["contact_a"])
        assert c.first_name == "DesktopEdited"


def test_desktop_crm_cross_tenant_update_denied(client, app, pwa_world):
    login(client, pwa_world["bob"])
    res = client.post(
        f"/api/contacts/{pwa_world['contact_a']}/update",
        data={"first_name": "Mallory"},
    )
    assert res.status_code == 404
    with app.app_context():
        c = db.session.get(Contact, pwa_world["contact_a"])
        assert c.first_name == "Alice's"
