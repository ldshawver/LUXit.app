import os

import pytest

from app import create_app
from extensions import db
from models import Company, Contact, ContactDuplicateExclusion, ContactIntelligenceJob, User, user_company


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove(); db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, is_admin=True):
    company = Company(name="Route Co")
    user = User(username="route", email="route@example.com", default_company_id=None, is_admin=is_admin)
    user.password_hash = "testpass"
    db.session.add_all([company, user]); db.session.flush()
    user.default_company_id = company.id
    db.session.execute(user_company.insert().values(user_id=user.id, company_id=company.id, is_default=True))
    db.session.commit()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id); sess["_fresh"] = True
    return user, company


def test_contacts_add_route_uses_canonical_resolution(client):
    _, company = login(client)
    resp1 = client.post("/contacts/add", data={"email":"one@example.com", "phone":"(916) 555-1212", "first_name":"One"}, follow_redirects=False)
    resp2 = client.post("/contacts/add", data={"email":"one@example.com", "phone":"+1 916 555 1212", "first_name":"Duplicate"}, follow_redirects=False)
    assert resp1.status_code in {302, 303}
    assert resp2.status_code in {302, 303}
    contacts = Contact.query.filter_by(company_id=company.id).all()
    assert len(contacts) == 1
    assert contacts[0].normalized_phone == "+19165551212"
    assert contacts[0].original_source == "manual_entry"
    assert contacts[0].phone_numbers.count() == 1


def test_contact_intelligence_job_api_requires_admin_and_resumes(client):
    _, company = login(client, is_admin=False)
    denied = client.post("/api/marketing/contacts/intelligence/jobs", json={"job_type":"phone_backfill"})
    assert denied.status_code == 403
    user = User.query.first(); user.is_admin = True; db.session.commit()
    db.session.add_all([
        Contact(company_id=company.id, phone="9165551212", is_active=True),
        Contact(company_id=company.id, phone="9165551213", is_active=True),
    ]); db.session.commit()
    created = client.post("/api/marketing/contacts/intelligence/jobs", json={"job_type":"phone_backfill", "batch_size":1, "dry_run":False})
    assert created.status_code == 200
    job_id = created.get_json()["job"]["id"]
    run1 = client.post(f"/api/marketing/contacts/intelligence/jobs/{job_id}/run", json={"max_batches":1})
    assert run1.status_code == 200
    assert run1.get_json()["job"]["processed"] == 1
    run2 = client.post(f"/api/marketing/contacts/intelligence/jobs/{job_id}/run", json={"max_batches":5})
    body = run2.get_json()["job"]
    assert body["processed"] == 2
    assert body["status"] == "completed"


def test_not_duplicate_pair_is_persisted_and_excluded(client):
    _, company = login(client)
    a = Contact(company_id=company.id, email="a@example.com", normalized_phone="+19165551212", is_active=True)
    b = Contact(company_id=company.id, email="b@example.com", normalized_phone="+19165551212", is_active=True)
    db.session.add_all([a,b]); db.session.commit()
    resp = client.post("/api/marketing/contacts/duplicates/not-duplicate", json={"contact_id_a":a.id, "contact_id_b":b.id})
    assert resp.status_code == 200
    assert ContactDuplicateExclusion.query.filter_by(company_id=company.id).count() == 1
    review = client.get("/api/marketing/contacts/duplicates")
    assert review.status_code == 200
    assert review.get_json()["count"] == 0


def test_google_status_and_suggestion_do_not_expose_tokens_or_overwrite_manual_name(client):
    _, company = login(client)
    status = client.get("/api/marketing/contacts/google/status")
    assert status.status_code == 200
    assert "token" not in str(status.get_json()).lower()
    contact = Contact(company_id=company.id, phone="+19165551212", normalized_phone="+19165551212", name="Manual Name", name_source="user", is_active=True)
    db.session.add(contact); db.session.commit()
    resp = client.post(f"/api/marketing/contacts/{contact.id}/google/suggestion", json={"action":"accept", "name":"Google Name"})
    assert resp.status_code == 409
    db.session.refresh(contact)
    assert contact.name == "Manual Name"


def test_public_newsletter_requires_trusted_company(client):
    resp = client.post("/newsletter-subscribe", json={"email":"public@example.com"})
    assert resp.status_code == 400
    assert Contact.query.filter_by(email="public@example.com").count() == 0


def test_public_newsletter_uses_explicit_company_and_stays_isolated(client):
    user, company = login(client)
    other = Company(name="Other Public Co")
    db.session.add(other); db.session.commit()
    resp = client.post(f"/newsletter-subscribe?company_id={company.id}", json={"email":"public@example.com"})
    assert resp.status_code == 200
    assert Contact.query.filter_by(company_id=company.id, email="public@example.com").count() == 1
    assert Contact.query.filter_by(company_id=other.id, email="public@example.com").count() == 0


def test_contact_delete_archives_and_restore_preserves_record(client):
    _, company = login(client)
    contact = Contact(company_id=company.id, email="archive@example.com", is_active=True, status="active")
    db.session.add(contact); db.session.commit()
    resp = client.post(f"/contacts/{contact.id}/delete", follow_redirects=False)
    assert resp.status_code in {302, 303}
    db.session.refresh(contact)
    assert contact.is_active is False
    assert contact.status == "archived"
    assert contact.archived_at is not None
    resp = client.post(f"/contacts/{contact.id}/restore", follow_redirects=False)
    assert resp.status_code in {302, 303}
    db.session.refresh(contact)
    assert contact.is_active is True
    assert contact.status == "active"
    assert contact.archived_at is None
