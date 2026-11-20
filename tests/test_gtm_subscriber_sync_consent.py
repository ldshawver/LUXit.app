"""The daily 'Email CRM - Daily Subscriber Sync' agent job must not undo an
opt-out (is_subscribed is still the legacy SMS send gate)."""
from datetime import datetime

import pytest

from app import create_app
from extensions import db
from models import Company, Contact
from services.subscriber_sync_service import SubscriberSyncService


@pytest.fixture
def app_ctx():
    app = create_app()
    app.config.update(TESTING=True)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_sync_never_resubscribes_opted_out_contacts(app_ctx):
    co = Company(name="Sync Co")
    db.session.add(co)
    db.session.flush()
    stopped = Contact(company_id=co.id, email="stop@x.example", segment="newsletter", is_subscribed=False,
                      is_active=True, sms_opted_out=True, sms_opt_out_at=datetime.utcnow())
    unsub = Contact(company_id=co.id, email="unsub@x.example", segment="newsletter", is_subscribed=False,
                    is_active=True, email_unsubscribed=True)
    fresh = Contact(company_id=co.id, email="fresh@x.example", segment="newsletter", is_subscribed=False, is_active=True)
    db.session.add_all([stopped, unsub, fresh])
    db.session.commit()

    result = SubscriberSyncService.sync_contacts_to_subscribers()

    assert result["synced_count"] == 0
    for c in (stopped, unsub, fresh):
        # Segment membership is not consent; nothing may flip the SMS gate.
        assert db.session.get(Contact, c.id).is_subscribed is False
