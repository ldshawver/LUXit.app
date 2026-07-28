"""PostgreSQL-only proofs for inbound identity hardening."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy import text

from app import create_app
from extensions import db
from models import (
    Company,
    Contact,
    ContactEmailAddress,
    ContactPhoneNumber,
    Segment,
    SegmentMember,
    SMSCampaign,
    SMSOutboundAttempt,
    SMSOutboundIntent,
    SMSRecipient,
    SMSRecipientDeliveryAttempt,
    TwilioAccount,
    TwilioConversation,
    TwilioMessage,
)
from services.contact_dedupe import merge_contacts
from services.contact_consent import (
    CANONICAL_EMAIL_OPTOUT_STATUS,
    has_explicit_email_opt_out,
)
from marketing_api import marketing_skip_reason
from services.contact_resolver import (
    discover_confirmation_candidate_ids,
    resolve_confirmation_identity,
)
from services.contact_identity import confirm_pending_identity
from services.sms_outbox import deliver_inbound_intents, enqueue_sms_intent


TEST_DB_NAME = "lux_identity_hardening_test"
TEST_DB_PORT = 5433


@pytest.fixture(scope="module")
def pg_app():
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL-only identity hardening tests")
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        assert db.engine.url.database == TEST_DB_NAME
        assert db.engine.url.port == TEST_DB_PORT
    return app


@pytest.fixture(autouse=True)
def clean_pg(pg_app):
    with pg_app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()


def _company(name="PG Identity"):
    row = Company(name=name)
    db.session.add(row)
    db.session.flush()
    return row


def _conversation(company, phone="+14155550100"):
    row = TwilioConversation(
        company_id=company.id, from_number=phone, to_number="+15559999999"
    )
    db.session.add(row)
    db.session.flush()
    return row


def _inbound(company, conversation, sid="SM-PG-IN"):
    row = TwilioMessage(
        company_id=company.id,
        conversation_id=conversation.id,
        twilio_sid=sid,
        direction="inbound",
        body="hello",
        processing_status="processing",
    )
    db.session.add(row)
    db.session.flush()
    return row


def _arm(contact, sid):
    now = datetime.utcnow()
    contact.identity_status = "awaiting_confirmation"
    contact.pending_first_name = "Luke"
    contact.pending_last_name = "Shawver"
    contact.pending_email = contact.email
    contact.identity_fields_requested_at = now
    contact.identity_last_request_sid = sid
    contact.identity_request_state = {
        "phase": "awaiting_confirmation",
        "company_id": contact.company_id,
        "contact_id": contact.id,
        "confirmation_nonce": sid,
        "requested_at": now.isoformat(),
    }


def test_both_twilio_routes_bind_same_handler_and_options(pg_app):
    rules = {
        rule.rule: rule
        for rule in pg_app.url_map.iter_rules()
        if rule.rule in {"/twilio/sms", "/twilio/sms/inbound"}
    }
    assert rules["/twilio/sms"].endpoint == "twilio.inbound_sms"
    assert rules["/twilio/sms/inbound"].endpoint == "twilio.inbound_sms"
    client = pg_app.test_client()
    for path in rules:
        response = client.post(path, data={"present": "but-required-fields-missing"})
        assert response.status_code == 400
        assert client.open(path, method="OPTIONS").status_code == 200


@pytest.mark.parametrize("path", ["/twilio/sms", "/twilio/sms/inbound"])
def test_twilio_routes_share_compliance_and_idsidempotency(pg_app, path):
    with pg_app.app_context():
        company = _company(path)
        account = TwilioAccount(company_id=company.id, from_phone="+15559999999", is_active=True)
        account.set_account_sid("ACtest")
        account.set_auth_token("token")
        db.session.add(account)
        db.session.commit()
    client = pg_app.test_client()
    payload = {
        "From": "+14155550123", "To": "+15559999999",
        "Body": "HELP", "MessageSid": f"SM-{path.rsplit('/', 1)[-1]}-HELP",
    }
    first = client.post(path, data=payload)
    second = client.post(path, data=payload)
    assert first.status_code == second.status_code == 200
    assert "Reply STOP" in first.get_data(as_text=True)
    assert second.get_data(as_text=True) == first.get_data(as_text=True)
    with pg_app.app_context():
        assert TwilioMessage.query.filter_by(twilio_sid=payload["MessageSid"]).count() == 1


def test_outbound_intent_delivers_once_and_records_attempt(pg_app, monkeypatch):
    with pg_app.app_context():
        company = _company()
        conv = _conversation(company)
        inbound = _inbound(company, conv)
        intent = enqueue_sms_intent(
            inbound_message_id=inbound.id, company_id=company.id,
            conversation_id=conv.id, effect_type="identity_reply",
            to_number=conv.from_number, body="Confirmed", is_auto_reply=True,
        )
        db.session.commit()
        calls = []

        def fake_send(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "success": True, "sid": "SM-OUT-ONCE",
                "provider_status": "queued", "from_number": conv.to_number,
            }

        monkeypatch.setattr("twilio_sms.sendConversationSms", fake_send)
        assert deliver_inbound_intents(inbound.id)["all_delivered"]
        assert deliver_inbound_intents(inbound.id)["all_delivered"]
        assert len(calls) == 1
        assert SMSOutboundIntent.query.get(intent.id).status == "delivered"
        assert SMSOutboundAttempt.query.filter_by(intent_id=intent.id).count() == 1
        assert TwilioMessage.query.filter_by(twilio_sid="SM-OUT-ONCE").count() == 1


def test_ambiguous_provider_attempt_is_not_blindly_retried(pg_app, monkeypatch):
    with pg_app.app_context():
        company = _company()
        conv = _conversation(company)
        inbound = _inbound(company, conv)
        intent = enqueue_sms_intent(
            inbound_message_id=inbound.id, company_id=company.id,
            conversation_id=conv.id, effect_type="owner_relay",
            to_number=conv.from_number, body="relay",
        )
        db.session.flush()
        intent.status = "sending"
        intent.attempt_count = 1
        db.session.add(SMSOutboundAttempt(
            intent_id=intent.id, attempt_number=1,
            attempt_key=f"{intent.idempotency_key}:1", status="sending",
        ))
        db.session.commit()
        monkeypatch.setattr(
            "twilio_sms.sendConversationSms",
            lambda *args, **kwargs: pytest.fail("ambiguous attempt was resent"),
        )
        result = deliver_inbound_intents(inbound.id)
        assert result["has_terminal"]
        assert db.session.get(SMSOutboundIntent, intent.id).status == "delivery_unknown"


def test_retryable_provider_failure_retries_same_intent(pg_app, monkeypatch):
    with pg_app.app_context():
        company = _company()
        conv = _conversation(company)
        inbound = _inbound(company, conv)
        intent = enqueue_sms_intent(
            inbound_message_id=inbound.id, company_id=company.id,
            conversation_id=conv.id, effect_type="campaign_reply",
            to_number=conv.from_number, body="reply",
        )
        db.session.commit()
        outcomes = iter([
            {"success": False, "delivery_class": "retryable", "error": "503", "error_code": "503"},
            {"success": True, "sid": "SM-OUT-RETRY", "provider_status": "queued"},
        ])
        monkeypatch.setattr("twilio_sms.sendConversationSms", lambda *a, **k: next(outcomes))
        assert deliver_inbound_intents(inbound.id)["has_retryable"]
        assert deliver_inbound_intents(inbound.id)["all_delivered"]
        assert SMSOutboundAttempt.query.filter_by(intent_id=intent.id).count() == 2


def test_sms_recipient_nulls_preserved_and_provider_history_lossless(pg_app):
    with pg_app.app_context():
        company = _company()
        canonical = Contact(company_id=company.id, is_active=True)
        duplicate = Contact(company_id=company.id, is_active=True)
        campaign = SMSCampaign(company_id=company.id, name="C", message="M", status="draft")
        segment = Segment(company_id=company.id, name="Shared")
        db.session.add_all([canonical, duplicate, campaign, segment])
        db.session.flush()
        db.session.add_all([
            SegmentMember(segment_id=segment.id, contact_id=canonical.id),
            SegmentMember(segment_id=segment.id, contact_id=duplicate.id),
            ContactEmailAddress(
                company_id=company.id, contact_id=canonical.id,
                normalized_value="shared@example.org", is_primary=True,
                verification_status="verified",
            ),
            ContactEmailAddress(
                company_id=company.id, contact_id=duplicate.id,
                normalized_value="SHARED@example.org", is_primary=True,
                verification_status="confirmed",
            ),
        ])
        rows = [
            SMSRecipient(company_id=company.id, campaign_id=None, contact_id=canonical.id,
                         provider_message_sid="SM-NULL-A", status="sent"),
            SMSRecipient(company_id=company.id, campaign_id=None, contact_id=duplicate.id,
                         provider_message_sid="SM-NULL-B", status="failed", error_message="network"),
            SMSRecipient(company_id=company.id, campaign_id=campaign.id, contact_id=canonical.id,
                         provider_message_sid="SM-CAMPAIGN-A", status="delivered",
                         delivered_at=datetime.utcnow()),
            SMSRecipient(company_id=company.id, campaign_id=campaign.id, contact_id=duplicate.id,
                         provider_message_sid="SM-CAMPAIGN-B", status="replied",
                         replied_at=datetime.utcnow(), provider_error_code="30001"),
        ]
        db.session.add_all(rows)
        db.session.commit()
        merge_contacts(canonical.id, [duplicate.id], company_id=company.id)
        assert SMSRecipient.query.filter_by(contact_id=canonical.id, campaign_id=None).count() == 2
        assert SMSRecipient.query.filter_by(contact_id=canonical.id, campaign_id=campaign.id).count() == 1
        assert SegmentMember.query.filter_by(contact_id=canonical.id, segment_id=segment.id).count() == 1
        assert ContactEmailAddress.query.filter_by(contact_id=canonical.id).count() == 1
        assert {
            row.provider_message_sid for row in SMSRecipientDeliveryAttempt.query.all()
        } == {"SM-NULL-A", "SM-NULL-B", "SM-CAMPAIGN-A", "SM-CAMPAIGN-B"}


@pytest.mark.parametrize(
    "primary_flags,duplicate_flags,expected",
    [
        ({"email_opt_in": True, "email_consent_status": "opted_in"}, {"do_not_email": True}, "unsubscribed"),
        ({"email_opt_in": True}, {"email_unsubscribed": True, "email_consent_status": "unknown"}, "unsubscribed"),
        ({"email_consent_status": "opted_out"}, {"email_consent_status": "opted_in"}, "unsubscribed"),
        ({"email_consent_status": "unsubscribed"}, {"email_consent_status": "opted_in"}, "unsubscribed"),
        ({"email_consent_status": "unknown"}, {"is_subscribed": False}, "unsubscribed"),
        ({"email_consent_status": "unknown"}, {"email_consent_status": "opted_in"}, "opted_in"),
        ({"email_consent_status": "opted_in"}, {"email_consent_status": "opted_in"}, "opted_in"),
        ({"email_consent_status": "unknown"}, {"email_consent_status": "unknown"}, "unknown"),
    ],
)
def test_email_consent_merge_precedence(pg_app, primary_flags, duplicate_flags, expected):
    with pg_app.app_context():
        company = _company()
        primary = Contact(company_id=company.id, is_active=True, **primary_flags)
        duplicate = Contact(company_id=company.id, is_active=True, **duplicate_flags)
        db.session.add_all([primary, duplicate])
        db.session.commit()
        merge_contacts(primary.id, [duplicate.id], company_id=company.id)
        assert primary.email_consent_status == expected
        if expected == CANONICAL_EMAIL_OPTOUT_STATUS:
            assert primary.do_not_email and primary.email_unsubscribed
            assert not primary.email_opt_in
            assert not primary.email_subscribed
            assert not primary.is_subscribed


@pytest.mark.parametrize("status", ["unsubscribed", "opted_out"])
def test_legacy_and_canonical_email_opt_out_states_are_suppressive(pg_app, status):
    with pg_app.app_context():
        company = _company()
        contact = Contact(
            company_id=company.id,
            email="suppressed@example.org",
            email_opt_in=True,
            email_consent_status=status,
            is_active=True,
        )
        db.session.add(contact)
        db.session.commit()
        assert has_explicit_email_opt_out(contact)
        assert marketing_skip_reason(contact, "email") == "email_unsubscribed"


def test_email_consent_merge_preserves_unsubscribe_provenance(pg_app):
    with pg_app.app_context():
        company = _company()
        unsubscribed_at = datetime(2025, 3, 1)
        primary = Contact(
            company_id=company.id,
            email_opt_in=True,
            email_consent_status="opted_in",
            marketing_preferences_source="signup_form",
            marketing_preferences_updated_at=datetime(2025, 2, 1),
            is_active=True,
        )
        duplicate = Contact(
            company_id=company.id,
            email_consent_status="opted_out",
            marketing_preferences_source="unsubscribe_link",
            marketing_preferences_reason="customer_request",
            marketing_preferences_updated_at=unsubscribed_at,
            is_active=True,
        )
        db.session.add_all([primary, duplicate])
        db.session.commit()
        merge_contacts(primary.id, [duplicate.id], company_id=company.id)
        assert primary.email_consent_status == CANONICAL_EMAIL_OPTOUT_STATUS
        assert primary.marketing_preferences_updated_at == unsubscribed_at
        assert "unsubscribe_link" in primary.marketing_preferences_source
        assert primary.marketing_preferences_reason == "customer_request"


@pytest.mark.parametrize(
    "primary_at,duplicate_at,expected",
    [
        (datetime(2025, 1, 1), datetime(2025, 2, 1), "duplicate"),
        (datetime(2025, 2, 1), datetime(2025, 1, 1), "primary"),
        (datetime(2025, 1, 1), datetime(2025, 1, 1), "duplicate"),
        (None, datetime(2025, 1, 1), "duplicate"),
        (None, None, "duplicate"),
    ],
)
def test_latest_source_uses_original_timestamps(pg_app, primary_at, duplicate_at, expected):
    with pg_app.app_context():
        company = _company()
        primary = Contact(
            company_id=company.id, is_active=True, latest_source="primary",
            last_touch_at=primary_at, original_source="first",
        )
        duplicate = Contact(
            company_id=company.id, is_active=True, latest_source="duplicate",
            last_touch_at=duplicate_at, original_source="second",
        )
        db.session.add_all([primary, duplicate])
        db.session.commit()
        merge_contacts(primary.id, [duplicate.id], company_id=company.id)
        assert primary.latest_source == expected
        assert primary.original_source == "first"


def test_case_insensitive_email_points_and_bounded_phone_lookup(pg_app):
    with pg_app.app_context():
        company = _company()
        target = Contact(
            company_id=company.id, is_active=True,
            phone="+14155550123", normalized_phone="+14155550123",
        )
        db.session.add(target)
        db.session.flush()
        db.session.add(ContactEmailAddress(
            company_id=company.id, contact_id=target.id,
            original_value=" Luke@Adiken.com ",
            normalized_value=" LUKE@ADIKEN.COM ",
            verification_status="confirmed", source="customer_confirmed",
        ))
        db.session.add_all([
            Contact(company_id=company.id, is_active=True, normalized_phone=f"+1415666{i:04d}")
            for i in range(1000)
        ])
        db.session.commit()
        ids = discover_confirmation_candidate_ids(
            company.id, normalized_phone="+14155550123",
            normalized_email="luke@adiken.com",
        )
        assert target.id in ids
        resolution = resolve_confirmation_identity(
            company.id, current_contact_id=-1, phone="+14155550123",
            email="luke@adiken.com", first_name="Luke", last_name="Shawver",
        )
        assert target.id in resolution.candidate_contact_ids
        db.session.execute(text("SET LOCAL enable_seqscan=off"))
        plan = db.session.execute(text(
            "EXPLAIN SELECT id FROM contact "
            "WHERE company_id=:company AND normalized_phone=:phone AND is_active IS TRUE"
        ), {"company": company.id, "phone": "+14155550123"}).scalars().all()
        assert any("Index" in line or "Bitmap" in line for line in plan), plan


def test_row_lock_blocks_second_connection(pg_app):
    url = os.environ["TEST_DATABASE_URL"]
    with pg_app.app_context():
        company = _company()
        contact = Contact(company_id=company.id, is_active=True)
        db.session.add(contact)
        db.session.commit()
        contact_id = contact.id
    first = psycopg2.connect(url)
    second = psycopg2.connect(url)
    try:
        first.autocommit = False
        second.autocommit = False
        with first.cursor() as cursor:
            cursor.execute("SELECT id FROM contact WHERE id=%s FOR UPDATE", (contact_id,))
        with second.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout='150ms'")
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                cursor.execute("SELECT id FROM contact WHERE id=%s FOR UPDATE", (contact_id,))
        second.rollback()
    finally:
        first.rollback()
        first.close()
        second.close()


def test_duplicate_intent_concurrency_has_one_row(pg_app):
    with pg_app.app_context():
        company = _company()
        conv = _conversation(company)
        inbound = _inbound(company, conv)
        db.session.commit()
        values = (company.id, conv.id, inbound.id)

    def insert():
        with pg_app.app_context():
            try:
                enqueue_sms_intent(
                    inbound_message_id=values[2], company_id=values[0],
                    conversation_id=values[1], effect_type="identity_reply",
                    to_number="+14155550100", body="same",
                )
                db.session.commit()
            except Exception:
                db.session.rollback()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: insert(), range(2)))
    with pg_app.app_context():
        assert SMSOutboundIntent.query.filter_by(inbound_message_id=values[2]).count() == 1


def test_duplicate_inbound_message_sid_concurrency_completes_once(pg_app):
    with pg_app.app_context():
        company = _company()
        account = TwilioAccount(company_id=company.id, from_phone="+15559999999", is_active=True)
        account.set_account_sid("ACtest")
        account.set_auth_token("token")
        db.session.add(account)
        db.session.commit()
    payload = {
        "From": "+14155550155", "To": "+15559999999",
        "Body": "HELP", "MessageSid": "SM-CONCURRENT-INBOUND",
    }

    def post():
        with pg_app.test_client() as client:
            return client.post("/twilio/sms/inbound", data=payload).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: post(), range(2)))
    assert 200 in statuses
    assert set(statuses).issubset({200, 500})
    with pg_app.app_context():
        rows = TwilioMessage.query.filter_by(twilio_sid=payload["MessageSid"]).all()
        assert len(rows) == 1
        assert rows[0].processing_status == "completed"


def test_concurrent_confirmations_targeting_one_canonical_are_serialized(pg_app):
    with pg_app.app_context():
        company = _company()
        canonical = Contact(
            company_id=company.id, is_active=True, identity_status="confirmed",
            first_name="Luke", last_name="Shawver", name="Luke Shawver",
            display_name="Luke Shawver", name_source="customer_confirmed",
            name_verification_level="verified",
            name_provenance={"source": "customer_confirmed_sms"},
            identity_verification_source="customer_confirmed",
        )
        db.session.add(canonical)
        db.session.flush()
        pairs = [
            ("+14155550131", "luke.one@adiken.com"),
            ("+14155550132", "luke.two@adiken.com"),
        ]
        pendings = []
        conversations = []
        for index, (phone, email) in enumerate(pairs):
            db.session.add_all([
                ContactPhoneNumber(
                    company_id=company.id, contact_id=canonical.id,
                    normalized_value=phone, verification_status="confirmed",
                    source="customer_confirmed",
                ),
                ContactEmailAddress(
                    company_id=company.id, contact_id=canonical.id,
                    normalized_value=email, verification_status="confirmed",
                    source="customer_confirmed",
                ),
            ])
            pending = Contact(
                company_id=company.id, is_active=True, phone=phone,
                normalized_phone=phone, email=email, normalized_email=email,
                source_provider="twilio",
            )
            db.session.add(pending)
            db.session.flush()
            _arm(pending, f"SM-REQUEST-{index}")
            conv = TwilioConversation(
                company_id=company.id, contact_id=pending.id,
                from_number=phone, to_number="+15559999999",
            )
            db.session.add(conv)
            db.session.flush()
            pendings.append(pending.id)
            conversations.append(conv.id)
        canonical_id = canonical.id
        db.session.commit()

    def confirm(index):
        with pg_app.app_context():
            contact = db.session.get(Contact, pendings[index])
            conv = db.session.get(TwilioConversation, conversations[index])
            result = confirm_pending_identity(contact, conv, f"SM-YES-{index}")
            db.session.commit()
            db.session.remove()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, range(2)))
    assert all(result.get("confirmed") for result in results)
    with pg_app.app_context():
        assert all(db.session.get(Contact, contact_id).merged_into_contact_id == canonical_id for contact_id in pendings)


def test_reciprocal_confirmation_lock_order_does_not_deadlock(pg_app):
    with pg_app.app_context():
        company = _company()
        contacts = []
        shared_conversation = None
        for index in range(2):
            contact = Contact(
                company_id=company.id, is_active=True,
                phone="+14155550140", normalized_phone="+14155550140",
                email="reciprocal@adiken.com", normalized_email="reciprocal@adiken.com",
                source_provider="twilio",
            )
            db.session.add(contact)
            db.session.flush()
            _arm(contact, f"SM-RECIPROCAL-{index}")
            db.session.add_all([
                ContactPhoneNumber(
                    company_id=company.id, contact_id=contact.id,
                    normalized_value=contact.phone, verification_status="confirmed",
                    source="customer_confirmed",
                ),
                ContactEmailAddress(
                    company_id=company.id, contact_id=contact.id,
                    normalized_value=contact.email, verification_status="confirmed",
                    source="customer_confirmed",
                ),
            ])
            if shared_conversation is None:
                shared_conversation = TwilioConversation(
                    company_id=company.id, contact_id=contact.id,
                    from_number=contact.phone, to_number="+15559999999",
                )
                db.session.add(shared_conversation)
                db.session.flush()
            contacts.append(contact.id)
        conversation_id = shared_conversation.id
        db.session.commit()

    def confirm(index):
        with pg_app.app_context():
            result = confirm_pending_identity(
                db.session.get(Contact, contacts[index]),
                db.session.get(TwilioConversation, conversation_id),
                f"SM-RECIPROCAL-YES-{index}",
            )
            db.session.commit()
            db.session.remove()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, range(2)))
    assert all(result.get("confirmed") or result.get("idempotent") for result in results)
