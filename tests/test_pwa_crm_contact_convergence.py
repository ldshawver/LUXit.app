"""PWA <-> CRM canonical-contact convergence (My Order slice, deliverable 2).

The shared write service (services.contact_profile.update_contact_fields),
the phone/email conflict-409 path, and the "ordinary edit leaves
consent/STOP/tags/segments/history untouched" guarantees are already covered
by tests/test_contact_profile.py. This file only adds the round-trip gaps that
were not proven:

  * a desktop-CRM name edit is findable by the new name in the PWA inbox
    SEARCH (which filters the denormalized TwilioConversation.contact_name),
    not just in the re-resolved display,
  * a PWA name edit is visible on the canonical CRM contact and in inbox
    search,
  * both edit surfaces emit the `contact_updated` SSE event that other
    tabs / a second session / the desktop CRM listen on,
  * a phone-collision edit is rejected without merging contacts or moving
    message history.

No SMS is sent. No production data is touched.
"""
import os

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import (
    Company,
    Contact,
    PhoneNumberUserPermission,
    SegmentMember,
    Segment,
    TwilioConversation,
    TwilioMessage,
    TwilioPhoneNumber,
    User,
    UserCompanyAccess,
)


@pytest.fixture
def world():
    os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
    os.environ.setdefault("TWILIO_AUTH_TOKEN", "authtest")
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")
    with app.app_context():
        db.drop_all()
        db.create_all()
        co = Company(name="Convergence Co", is_active=True)
        other = Company(name="Other Tenant", is_active=True)
        db.session.add_all([co, other])
        db.session.flush()
        staff = User(username="cv_staff", email="cv_staff@test.com",
                     password_hash=generate_password_hash("pw"), default_company_id=co.id)
        db.session.add(staff)
        db.session.flush()
        db.session.add(UserCompanyAccess(user_id=staff.id, company_id=co.id, role="admin",
                                         is_default=True, can_access_mobile_inbox=True))
        line = TwilioPhoneNumber(company_id=co.id, phone_number="+15550001000", friendly_name="Shared",
                                 sms_enabled=True, voice_enabled=True, is_active=True)
        db.session.add(line)
        db.session.flush()
        db.session.add(PhoneNumberUserPermission(company_id=co.id, user_id=staff.id, phone_number_id=line.id,
                                                 can_access_pwa=True, can_view_sms=True,
                                                 can_view_calls=True, can_view_voicemail=True))
        contact = Contact(company_id=co.id, first_name="Original", last_name="Name",
                          phone="+12025550130", normalized_phone="+12025550130", is_active=True)
        db.session.add(contact)
        db.session.flush()
        conv = TwilioConversation(company_id=co.id, phone_number_id=line.id, from_number="+12025550130",
                                  to_number="+15550001000", contact_id=contact.id, contact_name="Original Name",
                                  last_message_preview="hi", message_count=1)
        db.session.add(conv)
        db.session.flush()
        db.session.add(TwilioMessage(company_id=co.id, conversation_id=conv.id, direction="inbound",
                                     from_number="+12025550130", to_number="+15550001000",
                                     body="hi", twilio_sid="SM_hist_1"))
        db.session.commit()
        yield app, app.test_client(), {
            "co": co.id, "other": other.id, "staff": staff.id,
            "line": line.id, "contact": contact.id, "conv": conv.id,
        }
        db.session.remove()
        db.drop_all()


def _login(client, uid):
    with client.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(uid)
        s["_fresh"] = True


def _search(client, term):
    r = client.get(f"/api/inbox/conversations?filter=all&q={term}")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["conversations"]


# ── CRM edit -> PWA ──────────────────────────────────────────────────────────

def test_crm_name_edit_is_findable_in_pwa_search_and_display(world):
    app, client, ids = world
    _login(client, ids["staff"])

    # baseline: findable by old name, not the new one
    assert any(c["id"] == ids["conv"] for c in _search(client, "Original"))
    assert not _search(client, "Rebranded")

    r = client.post(f"/api/contacts/{ids['contact']}/update",
                    data={"first_name": "Rebranded", "last_name": "Customer"})
    assert r.status_code == 200, r.get_data(as_text=True)

    with app.app_context():
        c = db.session.get(Contact, ids["contact"])
        assert (c.first_name, c.last_name) == ("Rebranded", "Customer")

    hits = _search(client, "Rebranded")
    assert [c["id"] for c in hits] == [ids["conv"]]
    assert hits[0]["display_name"] == "Rebranded Customer"
    assert not _search(client, "Original")


# ── PWA edit -> CRM canonical ────────────────────────────────────────────────

def test_pwa_name_edit_updates_canonical_contact_and_search(world):
    app, client, ids = world
    _login(client, ids["staff"])

    r = client.patch(f"/api/inbox/conversations/{ids['conv']}/contact",
                     json={"first_name": "Pwa", "last_name": "Edited"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["contact"]["first_name"] == "Pwa"

    with app.app_context():
        c = db.session.get(Contact, ids["contact"])
        assert (c.first_name, c.last_name) == ("Pwa", "Edited")
        assert c.name_verification_level == "verified"

    hits = _search(client, "Pwa Edited")
    assert [c["id"] for c in hits] == [ids["conv"]]


# ── second-session / other-tab convergence contract ──────────────────────────

@pytest.mark.parametrize("surface", ["crm", "pwa"])
def test_both_edit_surfaces_emit_contact_updated_sse(world, monkeypatch, surface):
    app, client, ids = world
    _login(client, ids["staff"])
    events = []
    import inbox_pwa
    monkeypatch.setattr(inbox_pwa, "_push_sse_event",
                        lambda company_id, event, payload: events.append((company_id, event, payload)))

    if surface == "crm":
        r = client.post(f"/api/contacts/{ids['contact']}/update", data={"first_name": "Sse", "last_name": "Crm"})
    else:
        r = client.patch(f"/api/inbox/conversations/{ids['conv']}/contact", json={"first_name": "Sse", "last_name": "Pwa"})
    assert r.status_code == 200, r.get_data(as_text=True)

    contact_updates = [p for (_cid, ev, p) in events if ev == "contact_updated"]
    assert contact_updates, f"no contact_updated SSE from {surface} edit"
    assert contact_updates[0]["contact_id"] == ids["contact"]


# ── phone collision safety ──────────────────────────────────────────────────

def test_phone_collision_rejected_without_merge_or_history_move(world):
    app, client, ids = world
    _login(client, ids["staff"])
    with app.app_context():
        # a second canonical contact + its own conversation & message
        other_c = Contact(company_id=ids["co"], first_name="Second", last_name="Person",
                          phone="+12025559999", normalized_phone="+12025559999", is_active=True)
        db.session.add(other_c)
        db.session.flush()
        other_conv = TwilioConversation(company_id=ids["co"], phone_number_id=ids["line"],
                                        from_number="+12025559999", to_number="+15550001000",
                                        contact_id=other_c.id, contact_name="Second Person")
        db.session.add(other_conv)
        db.session.flush()
        db.session.add(TwilioMessage(company_id=ids["co"], conversation_id=other_conv.id, direction="inbound",
                                     from_number="+12025559999", to_number="+15550001000",
                                     body="hey", twilio_sid="SM_hist_2"))
        seg = Segment(company_id=ids["co"], name="VIP", segment_type="custom")
        db.session.add(seg)
        db.session.flush()
        db.session.add(SegmentMember(segment_id=seg.id, contact_id=other_c.id, source="manual"))
        db.session.commit()
        other_id, other_conv_id, seg_id = other_c.id, other_conv.id, seg.id

    # try to move `other_c` onto the first contact's number (differently formatted)
    r = client.post(f"/api/contacts/{other_id}/update", data={"phone": "(202) 555-0130"})
    assert r.status_code == 409
    assert r.get_json()["error"] == "contact_identity_conflict"
    assert r.get_json()["field"] == "phone"

    with app.app_context():
        assert db.session.get(Contact, other_id).normalized_phone == "+12025559999"
        assert db.session.get(Contact, other_id).merged_into_contact_id is None
        assert db.session.get(Contact, ids["contact"]).normalized_phone == "+12025550130"
        # history stays attached to its original contact/conversation
        m1 = TwilioMessage.query.filter_by(twilio_sid="SM_hist_1").one()
        m2 = TwilioMessage.query.filter_by(twilio_sid="SM_hist_2").one()
        assert m1.conversation_id == ids["conv"]
        assert m2.conversation_id == other_conv_id
        assert SegmentMember.query.filter_by(segment_id=seg_id, contact_id=other_id).count() == 1


def test_cross_tenant_same_phone_never_collides_or_leaks(world):
    app, client, ids = world
    _login(client, ids["staff"])
    with app.app_context():
        foreign = Contact(company_id=ids["other"], first_name="Foreign", last_name="Owner",
                          phone="+12025550130", normalized_phone="+12025550130", is_active=True)
        db.session.add(foreign)
        db.session.commit()

    # editing our contact's own number is fine even though another *tenant* has it
    r = client.post(f"/api/contacts/{ids['contact']}/update", data={"phone": "+1 202 555 0130", "first_name": "Still", "last_name": "Ours"})
    assert r.status_code == 200, r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Contact, ids["contact"]).normalized_phone == "+12025550130"
