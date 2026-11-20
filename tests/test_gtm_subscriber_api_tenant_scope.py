"""Subscriber / consent APIs, the contact-activities JSON API and contact
listings (lead scoring, event registrants) must stay inside the caller's
tenant. On 49b40ff a tenant-A viewer could subscribe or
unsubscribe tenant-B contacts (the response echoed their email) and run the
all-tenant subscription sweep."""
from extensions import db
from datetime import datetime

from models import Contact, ContactActivity, Event, EventRegistration

from tests.test_gtm_p0_security import world  # noqa: F401  (shared fixture)


def _own_contact(ids):
    c = Contact(company_id=ids["a"], first_name="Ann", email="ann@a.example", is_subscribed=False, segment="newsletter")
    db.session.add(c)
    db.session.commit()
    return c.id


def test_cannot_subscribe_or_unsubscribe_other_tenant_contact(world):
    app, client_for, ids, _ = world
    c = client_for(ids["viewer_a"])
    for action, start in (("subscribe", False), ("unsubscribe", True)):
        cb = db.session.get(Contact, ids["contact_b"])
        cb.is_subscribed = start
        db.session.commit()
        resp = c.post(f"/api/contacts/{ids['contact_b']}/{action}", json={})
        assert resp.status_code == 404
        assert "bea@b.example" not in resp.get_data(as_text=True)
        db.session.expire_all()
        assert db.session.get(Contact, ids["contact_b"]).is_subscribed is start
    resp = c.post("/api/contacts/bulk-subscribe", json={"contact_ids": [ids["contact_b"]]})
    assert resp.get_json()["subscribed_count"] == 0
    db.session.expire_all()
    assert db.session.get(Contact, ids["contact_b"]).is_subscribed is True  # left as the loop set it; untouched
    resp = c.post("/api/contacts/bulk-unsubscribe", json={"contact_ids": [ids["contact_b"]]})
    assert resp.get_json()["unsubscribed_count"] == 0
    db.session.expire_all()
    assert db.session.get(Contact, ids["contact_b"]).is_subscribed is True


def test_tenant_user_cannot_run_all_tenant_subscriber_sweeps(world):
    app, client_for, ids, _ = world
    cb = db.session.get(Contact, ids["contact_b"])
    cb.is_subscribed, cb.segment = False, "newsletter"
    db.session.commit()
    c = client_for(ids["owner_a"])
    for path in ("/api/subscribers/sync", "/api/subscribers/sync-contacts-to-subscribers"):
        assert c.post(path, json={}).status_code == 403
    for path in ("/api/subscribers", "/api/subscribers/stats"):
        assert c.get(path).status_code == 403
    db.session.expire_all()
    assert not db.session.get(Contact, ids["contact_b"]).is_subscribed


def test_cannot_read_other_tenant_activities_via_json_api(world):
    app, client_for, ids, _ = world
    db.session.add(ContactActivity(contact_id=ids["contact_b"], company_id=ids["b"], activity_type="note", title="B private"))
    db.session.commit()
    resp = client_for(ids["owner_a"]).get(f"/api/contacts/{ids['contact_b']}/activities")
    assert resp.status_code == 404
    assert "B private" not in resp.get_data(as_text=True)


def test_own_tenant_subscription_and_activity_api_still_work(world):
    app, client_for, ids, _ = world
    own = _own_contact(ids)
    c = client_for(ids["owner_a"])
    assert c.post(f"/api/contacts/{own}/subscribe", json={}).get_json()["success"] is True
    db.session.expire_all()
    assert db.session.get(Contact, own).is_subscribed
    assert c.post("/api/contacts/bulk-unsubscribe", json={"contact_ids": [own]}).get_json()["unsubscribed_count"] == 1
    assert c.get(f"/api/contacts/{own}/activities").status_code == 200


def test_platform_admin_keeps_subscriber_sweep(world):
    app, client_for, ids, _ = world
    assert client_for(ids["platform"]).post("/api/subscribers/sync-contacts-to-subscribers", json={}).status_code == 200


def test_lead_scoring_lists_only_own_tenant_contacts(world):
    app, client_for, ids, _ = world
    _own_contact(ids)
    body = client_for(ids["owner_a"]).get("/lead-scoring").get_data(as_text=True)
    assert "ann@a.example" in body
    assert "bea@b.example" not in body


def test_event_page_hides_other_tenant_registrants(world):
    app, client_for, ids, _ = world
    own = _own_contact(ids)
    ev = Event(name="Shared event", start_date=datetime(2026, 11, 1))
    db.session.add(ev)
    db.session.flush()
    db.session.add_all([EventRegistration(event_id=ev.id, contact_id=own),
                        EventRegistration(event_id=ev.id, contact_id=ids["contact_b"])])
    db.session.commit()
    body = client_for(ids["owner_a"]).get(f"/events/{ev.id}").get_data(as_text=True)
    assert "ann@a.example" in body
    assert "bea@b.example" not in body
