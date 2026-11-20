"""Regression: a missing email opt-in must never read as an email unsubscribe.

Production defect (2026-10): Google Contacts import sets ``is_subscribed=False``
on every new contact. has_explicit_email_opt_out() treated that as an email
opt-out, so the UI showed "Suppressed"/"Email Unsubscribed" for ~2150 contacts,
and contact_dedupe escalated it into a full apply_email_opt_out() on merge
survivors (do_not_email, email_unsubscribed, email_consent_status=unsubscribed).
"""
import os

import pytest

from app import create_app
from extensions import db
from models import Company, Contact, Segment, SegmentMember
from services.contact_consent import (
    apply_email_opt_out,
    has_explicit_email_opt_out,
    is_email_contactable,
    suppression_badges,
)


@pytest.fixture
def app():
    os.environ["FLASK_ENV"] = "testing"
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def company(app):
    co = Company(name="Consent Co")
    db.session.add(co)
    db.session.commit()
    return co


def _google_import_contact(company, **kw):
    """Shape of a freshly imported Google contact (google_contacts.py)."""
    fields = dict(company_id=company.id, email="landon@example.com", phone="+15550001978",
                  normalized_phone="+15550001978", source="google_contacts", is_active=True,
                  is_subscribed=False)
    fields.update(kw)
    return Contact(**fields)


def _stop_contact(company):
    """Shape left by twilio_sms STOP + sms_keyword_engine.mark_opt_out."""
    from datetime import datetime
    return Contact(company_id=company.id, email="stop@example.com", phone="+15550002099",
                   normalized_phone="+15550002099", is_active=True, is_subscribed=False,
                   sms_opted_out=True, sms_opt_out_at=datetime.utcnow(),
                   sms_consent_status="opted_out", tags="sms_opt_out")


def test_missing_opt_in_is_not_email_unsubscribe(company):
    from marketing_api import marketing_skip_reason
    c = _google_import_contact(company)
    db.session.add(c); db.session.commit()
    assert not has_explicit_email_opt_out(c)
    assert is_email_contactable(c)
    assert marketing_skip_reason(c, "email") is None
    assert suppression_badges(c) == []


def test_affirmative_email_opt_out_still_suppresses(company):
    from marketing_api import marketing_skip_reason
    c = _google_import_contact(company)
    apply_email_opt_out(c)
    db.session.add(c); db.session.commit()
    assert has_explicit_email_opt_out(c)
    assert not is_email_contactable(c)
    assert marketing_skip_reason(c, "email") == "do_not_email"
    assert suppression_badges(c) == ["Do Not Email", "Email Unsubscribed"]
    # Email opt-out does not touch SMS consent.
    assert marketing_skip_reason(c, "sms") is None
    assert not c.sms_opted_out and not c.do_not_sms


@pytest.mark.parametrize("flags,badge", [
    ({"do_not_email": True}, "Do Not Email"),
    ({"email_unsubscribed": True}, "Email Unsubscribed"),
    ({"email_subscribed": False}, "Email Unsubscribed"),
    ({"email_consent_status": "opted_out"}, "Email Unsubscribed"),
])
def test_each_legitimate_email_signal_is_enforced(company, flags, badge):
    from marketing_api import marketing_skip_reason
    c = _google_import_contact(company, **flags)
    db.session.add(c); db.session.commit()
    assert marketing_skip_reason(c, "email") in {"do_not_email", "email_unsubscribed"}
    assert badge in suppression_badges(c)


def test_sms_stop_stays_sms_suppressed_without_manufacturing_email_unsubscribe(company):
    from marketing_api import _contact_has_sms_consent, marketing_skip_reason
    c = _stop_contact(company)
    db.session.add(c); db.session.commit()
    assert marketing_skip_reason(c, "sms") == "sms_opted_out"
    assert not _contact_has_sms_consent(c)
    assert marketing_skip_reason(c, "email") is None
    assert suppression_badges(c) == ["SMS Opted Out"]


def test_merge_does_not_escalate_missing_opt_in_into_unsubscribe(company):
    """Luke's exact path: clean primary + Google-imported duplicate merged."""
    from services.contact_dedupe import merge_contacts
    primary = Contact(company_id=company.id, email="luke@example.com", phone="+15550000001",
                      normalized_phone="+15550000001", is_active=True, is_subscribed=True,
                      sms_consent_status="opted_in", sms_marketing_opt_in=True)
    dup = _google_import_contact(company, email="luke@example.com", phone="+15550000002",
                                 normalized_phone="+15550000002")
    db.session.add_all([primary, dup]); db.session.commit()
    merge_contacts(primary.id, [dup.id], company_id=company.id)
    db.session.refresh(primary)
    assert not primary.do_not_email
    assert not primary.email_unsubscribed
    assert primary.email_subscribed is True
    assert primary.email_consent_status in (None, "unknown")
    assert suppression_badges(primary) == []
    assert primary.sms_marketing_opt_in is True


def test_merge_still_propagates_real_opt_outs(company):
    from services.contact_dedupe import merge_contacts
    primary = _google_import_contact(company, email="a@example.com", phone="+15550000011",
                                     normalized_phone="+15550000011")
    dup = _stop_contact(company)
    dup.do_not_email = True
    db.session.add_all([primary, dup]); db.session.commit()
    merge_contacts(primary.id, [dup.id], company_id=company.id)
    db.session.refresh(primary)
    assert primary.do_not_email and primary.email_unsubscribed
    assert primary.sms_opted_out and primary.do_not_sms


def test_operator_unsubscribe_writes_canonical_opt_out(company):
    from services.subscriber_sync_service import SubscriberSyncService
    c = _google_import_contact(company, is_subscribed=True)
    db.session.add(c); db.session.commit()
    assert SubscriberSyncService.unsubscribe_contact(c.id)["success"]
    db.session.refresh(c)
    assert has_explicit_email_opt_out(c)
    assert c.do_not_email and c.email_unsubscribed and c.email_consent_status == "unsubscribed"
    assert c.marketing_preferences_source == "operator_unsubscribe"
    assert not c.sms_opted_out


def test_segment_badges_and_metrics_agree_with_send_gate(company):
    from marketing_api import marketing_skip_reason
    from routes import _segment_detail_metrics, _segment_member_view
    seg = Segment(company_id=company.id, name="My Order Customer", is_active=True)
    landon = _google_import_contact(company)
    luke = Contact(company_id=company.id, email="luke@example.com", phone="+15550000001",
                   normalized_phone="+15550000001", is_active=True, is_subscribed=True)
    stop = _stop_contact(company)
    unsub = _google_import_contact(company, email="u@example.com", phone="+15550000099",
                                   normalized_phone="+15550000099")
    apply_email_opt_out(unsub)
    db.session.add_all([seg, landon, luke, stop, unsub]); db.session.flush()
    members = {}
    for c in (landon, luke, stop, unsub):
        m = SegmentMember(segment_id=seg.id, contact_id=c.id, source="dynamic_rule")
        db.session.add(m); members[c.id] = m
    db.session.commit()

    assert _segment_member_view(members[landon.id])["badges"] == ["Dynamic"]
    assert _segment_member_view(members[luke.id])["badges"] == ["Dynamic"]
    assert _segment_member_view(members[stop.id])["badges"] == ["Dynamic", "Suppressed", "SMS Opted Out"]
    assert _segment_member_view(members[unsub.id])["badges"] == ["Dynamic", "Suppressed", "Do Not Email", "Email Unsubscribed"]

    metrics = _segment_detail_metrics(seg)
    send_ok = [c for c in (landon, luke, stop, unsub) if marketing_skip_reason(c, "email") is None]
    assert metrics["email_eligible"] == len(send_ok) == 3
    assert metrics["email_unsubscribed"] == 1
    assert metrics["suppressed_contacts"] == 2


def test_tenant_isolation_of_marketing_preferences(app, company):
    other = Company(name="Other Tenant")
    db.session.add(other); db.session.commit()
    foreign = _google_import_contact(other)
    db.session.add(foreign); db.session.commit()
    from marketing_api import _contact_or_404
    from werkzeug.exceptions import NotFound
    with pytest.raises(NotFound):
        _contact_or_404(foreign.id, company.id)
