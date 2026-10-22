"""Canonical contact editing from PWA conversations — completion checks.

Two gaps this release closes:
  1. The contact panel / edit mode only rendered on a docked >=900px column;
     on a phone it must be reachable as a full-screen slide-over.
  2. A PWA-verified edit must set durable name provenance so a later Google/iOS
     sync cannot outrank or silently replace an operator-entered name.

The edit route (PATCH /api/inbox/conversations/<id>/contact), the conflict-409
path, and services.contact_profile.update_contact_fields already exist and are
covered by tests/test_contact_profile.py — this file only adds the two gaps.
"""
import os
from pathlib import Path

import pytest

from app import create_app
from extensions import db
from models import Company, Contact, GoogleContactLookup, User

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "templates/inbox_pwa/index.html").read_text()


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    a = create_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with a.app_context():
        db.drop_all()
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


def _company(name="Edit Co"):
    c = Company(name=name)
    db.session.add(c)
    db.session.flush()
    return c


def _contact(co, **kw):
    kw.setdefault("phone", "+12025550170")
    kw.setdefault("normalized_phone", kw["phone"])
    c = Contact(company_id=co.id, is_active=True, **kw)
    db.session.add(c)
    db.session.flush()
    return c


# ── 1. mobile reachability (template contract) ────────────────────────────────

def test_contact_panel_has_mobile_slide_over_and_close_control():
    # A close affordance exists in the panel head…
    assert 'class="cp-close"' in INDEX_HTML
    assert "closeContactPanel()" in INDEX_HTML
    # …shown only on the phone-width slide-over, where the panel is a full
    # overlay rather than a 260px column.
    assert "@media (max-width: 899px)" in INDEX_HTML
    assert "#contactPanel .cp-close { display: inline-flex; }" in INDEX_HTML
    # toggle + esc + auto-close on navigation.
    assert "function toggleContactPanel()" in INDEX_HTML
    assert "if (e.key === 'Escape'" in INDEX_HTML
    body = INDEX_HTML.split("async function openConversation(")[1].split("}")[0]
    assert "closeContactPanel()" in body


# ── 2. PWA-verified edit provenance beats later enrichment ────────────────────

def test_pwa_verified_edit_records_durable_provenance(app):
    from services.contact_profile import update_contact_fields
    co = _company()
    c = _contact(co, first_name="Bob", last_name="Old")
    update_contact_fields(
        c, company_id=co.id,
        fields={"first_name": "Robert", "last_name": "Client"}, source="pwa_verified",
        actor_user_id=7,
    )
    assert c.name_source == "pwa_verified"
    assert c.name_verification_level == "verified"
    assert c.name_verified_at is not None
    assert (c.name_provenance or {}).get("source") == "pwa_verified"


def test_manual_edit_also_marks_verified(app):
    from services.contact_profile import update_contact_fields
    co = _company()
    c = _contact(co, first_name="A", phone="+12025550171", normalized_phone="+12025550171")
    update_contact_fields(c, company_id=co.id, fields={"first_name": "Alice"}, source="manual")
    assert c.name_verification_level == "verified"


def test_pwa_verified_name_survives_google_lookup_enrichment(app):
    from services.contact_profile import update_contact_fields
    from services.contact_resolver import resolve_contact_identity
    co = _company()
    u = User(username="op@example.com", email="op@example.com")
    u.password_hash = "x"
    db.session.add(u)
    db.session.flush()
    c = _contact(co, first_name="Old", last_name="Name",
                 phone="+12025550172", normalized_phone="+12025550172")

    update_contact_fields(
        c, company_id=co.id,
        fields={"first_name": "Operator", "last_name": "Entered"}, source="pwa_verified",
        actor_user_id=u.id,
    )
    db.session.flush()

    # A Google lookup row now claims a different name for the same number.
    db.session.add(GoogleContactLookup(
        company_id=co.id, user_id=u.id, normalized_phone="+12025550172",
        display_name="Google Sourced", is_ambiguous=False, candidate_count=1,
    ))
    db.session.flush()

    resolve_contact_identity(co.id, contact_id=c.id, allow_enrichment=True)
    db.session.flush()
    db.session.refresh(c)
    assert c.first_name == "Operator"
    assert c.last_name == "Entered"
    assert c.name_source == "pwa_verified"
