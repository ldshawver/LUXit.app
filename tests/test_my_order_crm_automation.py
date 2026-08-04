import os

import pytest
from werkzeug.security import generate_password_hash

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "g2CDXwdc6VKAElQ5QWqFBCsmXL_dQAs3e44_Gl1oJaU=")

from app import create_app
from extensions import db
from models import Company, Contact, CRMAutomationExecution, Segment, SegmentMember, User, UserCompanyAccess
from services.crm_automation import (
    AutomationContractError,
    DuplicateCanonicalRecord,
    FIRST_INBOUND_TRIGGER,
    MY_ORDER_CUSTOMER_NAME,
    TAG_ADDED_TRIGGER,
    assign_contact_tag,
    backfill_preview,
    dispatch_event,
    ensure_my_order_automation,
    validate_definition,
)


@pytest.fixture
def crm_app():
    app = create_app()
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        company = Company(name="My Order CRM Tenant")
        other = Company(name="Other CRM Tenant")
        db.session.add_all([company, other])
        db.session.commit()
        yield app, company, other
        db.session.remove()
        db.drop_all()


def _contact(company, **values):
    contact = Contact(company_id=company.id, is_active=True, **values)
    db.session.add(contact)
    db.session.flush()
    return contact


def _records(company):
    return ensure_my_order_automation(company.id)


def _first_event(company, contact, key="twilio:SM-FIRST", first=True):
    return dispatch_event(
        company.id, contact.id, FIRST_INBOUND_TRIGGER, key,
        direction="inbound", channel="sms", first_inbound_sms=first,
        message_sid=key.removeprefix("twilio:"),
    )


def test_ui_json_is_the_same_definition_executed_by_server(crm_app):
    import json
    app, company, _ = crm_app
    records = _records(company)
    user = User(username="crm-json-admin", email="crm-json@example.test",
                password_hash=generate_password_hash("password"), is_admin=True,
                default_company_id=company.id)
    db.session.add(user)
    db.session.flush()
    db.session.add(UserCompanyAccess(user_id=user.id, company_id=company.id, role="admin", is_default=True))
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True
    form = {
        "name": "UI Contract Rule",
        "segment_type": "automation_rule",
        "match_mode": "all",
        "triggers": json.dumps([TAG_ADDED_TRIGGER]),
        "conditions": json.dumps({"tag_ids": [records["tag"].id]}),
        "actions": json.dumps([{"type": "segment.add_contact", "segment_id": records["segment"].id}]),
        "is_dynamic": "on",
    }
    response = client.post("/segments/create", data=form)
    assert response.status_code == 302
    persisted = Segment.query.filter_by(company_id=company.id, name="UI Contract Rule").one()
    assert persisted.triggers == json.loads(form["triggers"])
    assert persisted.conditions == json.loads(form["conditions"])
    assert persisted.actions == json.loads(form["actions"])
    contact = _contact(company)
    dispatch_event(company.id, contact.id, TAG_ADDED_TRIGGER, "ui-contract:1", tag_id=records["tag"].id)
    assert CRMAutomationExecution.query.filter_by(rule_id=persisted.id, contact_id=contact.id).count() == 1


def test_exact_json_contract_is_valid_and_unknown_vocabulary_is_rejected(crm_app):
    _, company, _ = crm_app
    records = _records(company)
    rule1, rule2 = records["rules"]
    assert rule1.triggers == ["inbound_sms.first_for_contact"]
    assert rule1.conditions == {"direction": "inbound", "channel": "sms", "first_inbound_sms": True,
                                "contact_state": "canonical_active"}
    assert rule1.actions == [{"type": "contact.add_tag", "tag_id": records["tag"].id}]
    assert rule2.triggers == ["tag_added"]
    assert rule2.conditions == {"tag_ids": [records["tag"].id]}
    assert rule2.actions == [{"type": "segment.add_contact", "segment_id": records["segment"].id}]
    validate_definition(rule1.triggers, rule1.conditions, rule1.actions, "all")
    with pytest.raises(AutomationContractError):
        validate_definition(["first_inbound_sms"], {}, [{"type": "add_tag", "tag_id": 1}], "all")


def test_first_inbound_adds_tag_then_segment_once(crm_app):
    _, company, _ = crm_app
    records = _records(company)
    contact = _contact(company, phone="+15550000001")
    _first_event(company, contact)
    db.session.flush()
    assert MY_ORDER_CUSTOMER_NAME in contact.tags
    assert SegmentMember.query.filter_by(segment_id=records["segment"].id, contact_id=contact.id).count() == 1
    assert CRMAutomationExecution.query.filter_by(company_id=company.id).count() == 2


def test_second_sms_and_duplicate_sid_do_not_repeat_actions(crm_app):
    _, company, _ = crm_app
    records = _records(company)
    contact = _contact(company)
    _first_event(company, contact)
    initial = CRMAutomationExecution.query.count()
    _first_event(company, contact, key="twilio:SM-SECOND", first=False)
    _first_event(company, contact, key="twilio:SM-FIRST", first=True)
    assert CRMAutomationExecution.query.count() == initial
    assert SegmentMember.query.filter_by(segment_id=records["segment"].id, contact_id=contact.id).count() == 1


@pytest.mark.parametrize("source", ["manual", "api", "campaign", "automation", "merge"])
def test_every_tag_source_adds_membership_idempotently(crm_app, source):
    _, company, _ = crm_app
    records = _records(company)
    contact = _contact(company)
    assert assign_contact_tag(contact, MY_ORDER_CUSTOMER_NAME, source=source, event_key=f"{source}:1") is True
    assert assign_contact_tag(contact, MY_ORDER_CUSTOMER_NAME, source=source, event_key=f"{source}:1") is False
    assert SegmentMember.query.filter_by(segment_id=records["segment"].id, contact_id=contact.id).count() == 1


def test_imported_tag_assignment_dispatches_tag_added(crm_app):
    _, company, _ = crm_app
    from services.contact_audience import upsert_contact_from_source
    contact = upsert_contact_from_source(
        company.id, email="import@example.test", tags=[MY_ORDER_CUSTOMER_NAME], source_channel="csv_import"
    )
    records = _records(company)
    assert SegmentMember.query.filter_by(segment_id=records["segment"].id, contact_id=contact.id).count() == 1


@pytest.mark.parametrize("legacy", ["My Order", "MyOrder Customer", " my order   customer "])
def test_legacy_tag_variants_normalize_to_exact_canonical_label(crm_app, legacy):
    _, company, _ = crm_app
    contact = _contact(company, tags=legacy)
    from services.crm_automation import synchronize_contact_tags
    synchronize_contact_tags(contact, source="legacy_import")
    assert contact.tags == MY_ORDER_CUSTOMER_NAME
    records = _records(company)
    assert SegmentMember.query.filter_by(segment_id=records["segment"].id, contact_id=contact.id).count() == 1


def test_bare_myorder_is_not_a_customer_tag_alias(crm_app):
    _, company, _ = crm_app
    contact = _contact(company, tags="MyOrder")
    from services.crm_automation import synchronize_contact_tags
    synchronize_contact_tags(contact, source="legacy_import")
    assert contact.tags == "MyOrder"
    assert SegmentMember.query.count() == 0


def test_existing_and_multiple_executions_keep_one_membership(crm_app):
    _, company, _ = crm_app
    records = _records(company)
    contact = _contact(company)
    db.session.add(SegmentMember(segment_id=records["segment"].id, contact_id=contact.id, source="manual"))
    db.session.flush()
    assign_contact_tag(contact, MY_ORDER_CUSTOMER_NAME, event_key="manual:a")
    dispatch_event(company.id, contact.id, TAG_ADDED_TRIGGER, "manual:b", tag_id=records["tag"].id)
    assert SegmentMember.query.filter_by(segment_id=records["segment"].id, contact_id=contact.id).count() == 1


def test_foreign_tenant_references_are_rejected(crm_app):
    _, company, other = crm_app
    records = _records(company)
    foreign = _records(other)
    contact = _contact(company)
    records["rules"][0].actions = [{"type": "contact.add_tag", "tag_id": foreign["tag"].id}]
    with pytest.raises(AutomationContractError, match="not owned"):
        _first_event(company, contact)
    assert not contact.tags


def test_merge_preserves_tag_and_consolidates_memberships(crm_app):
    _, company, _ = crm_app
    records = _records(company)
    primary = _contact(company, email="primary@example.test")
    duplicate = _contact(company, email="duplicate@example.test", tags=MY_ORDER_CUSTOMER_NAME)
    db.session.add_all([
        SegmentMember(segment_id=records["segment"].id, contact_id=primary.id, source="manual"),
        SegmentMember(segment_id=records["segment"].id, contact_id=duplicate.id, source="import"),
    ])
    db.session.commit()
    from services.contact_dedupe import merge_contacts
    merge_contacts(primary.id, [duplicate.id], company_id=company.id)
    assert MY_ORDER_CUSTOMER_NAME in primary.tags
    assert SegmentMember.query.filter_by(segment_id=records["segment"].id, contact_id=primary.id).count() == 1
    assert duplicate.merged_into_contact_id == primary.id


def test_inactive_and_merged_away_contact_policy(crm_app):
    _, company, _ = crm_app
    records = _records(company)
    inactive = _contact(company)
    inactive.is_active = False
    result = dispatch_event(company.id, inactive.id, TAG_ADDED_TRIGGER, "inactive", tag_id=records["tag"].id)
    assert result["skipped"] == "inactive_contact"
    assert SegmentMember.query.filter_by(segment_id=records["segment"].id, contact_id=inactive.id).count() == 0
    canonical = _contact(company)
    inactive.merged_into_contact_id = canonical.id
    assign_contact_tag(inactive, MY_ORDER_CUSTOMER_NAME, source="merge", event_key="merged-away")
    assert SegmentMember.query.filter_by(segment_id=records["segment"].id, contact_id=canonical.id).count() == 1


def test_missing_records_created_once_and_normalized_duplicates_reported(crm_app):
    _, company, _ = crm_app
    first = _records(company)
    second = _records(company)
    assert first["tag"].id == second["tag"].id
    assert first["segment"].id == second["segment"].id
    db.session.add(Segment(company_id=company.id, name="  my order   CUSTOMER ", segment_type="contact_tag"))
    db.session.flush()
    with pytest.raises(DuplicateCanonicalRecord) as exc:
        _records(company)
    assert {row["id"] for row in exc.value.records} == {
        first["tag"].id,
        Segment.query.filter_by(company_id=company.id, name="  my order   CUSTOMER ").one().id,
    }


def test_backfill_preview_is_read_only_and_excludes_inactive_merged_and_other_tenant(crm_app):
    _, company, other = crm_app
    records = _records(company)
    tagged = _contact(company, tags=MY_ORDER_CUSTOMER_NAME)
    present = _contact(company, tags=MY_ORDER_CUSTOMER_NAME)
    inactive = _contact(company, tags=MY_ORDER_CUSTOMER_NAME)
    inactive.is_active = False
    merged = _contact(company, tags=MY_ORDER_CUSTOMER_NAME)
    merged.is_active = False
    merged.merged_into_contact_id = present.id
    foreign = _contact(other, tags=MY_ORDER_CUSTOMER_NAME)
    db.session.add(SegmentMember(segment_id=records["segment"].id, contact_id=present.id, source="manual"))
    db.session.flush()
    before = SegmentMember.query.count()
    rule_snapshots = {rule.id: (rule.triggers, rule.conditions, rule.actions, rule.updated_at) for rule in records["rules"]}
    preview = backfill_preview(company.id)
    assert preview["tagged_contact_count"] == 4
    assert preview["already_in_segment"] == 1
    assert preview["missing_from_segment"] == 1
    assert preview["inactive_contacts"] == 2
    assert preview["merged_contacts"] == 1
    assert preview["proposed_inserts"] == 1
    assert preview["contact_ids"] == [tagged.id]
    assert foreign.id not in preview["contact_ids"]
    assert SegmentMember.query.count() == before
    assert {rule.id: (rule.triggers, rule.conditions, rule.actions, rule.updated_at) for rule in records["rules"]} == rule_snapshots


def test_duplicate_segment_records_report_membership_counts(crm_app):
    _, company, _ = crm_app
    records = _records(company)
    duplicate = Segment(company_id=company.id, name="my order customer", segment_type="behavioral")
    db.session.add(duplicate)
    db.session.flush()
    with pytest.raises(DuplicateCanonicalRecord) as exc:
        _records(company)
    assert exc.value.role == "segment"
    assert {row["id"] for row in exc.value.records} == {records["segment"].id, duplicate.id}
