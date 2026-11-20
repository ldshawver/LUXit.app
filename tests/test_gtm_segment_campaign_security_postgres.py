"""PostgreSQL security + regression proofs for the GTM segment / SMS campaign
increment: guided segments, membership, tagging, export, campaign audiences,
sender enforcement, consent precedence, idempotent rebuilds, rollback,
structured errors and first-party analytics events.

PostgreSQL only (TEST_DATABASE_URL=postgresql...). The database name must end
in ``_test``: every test drops and recreates the schema. No provider call is
made (SMSService.send_sms is faked).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest
from flask import g
from itsdangerous import URLSafeTimedSerializer

PG_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL.startswith("postgresql"), reason="PostgreSQL-only security suite")


@pytest.fixture(scope="module")
def pg_app():
    os.environ["FLASK_ENV"] = "testing"
    from app import create_app
    from extensions import db
    application = create_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with application.app_context():
        assert db.engine.dialect.name == "postgresql"
        assert str(db.engine.url.database).endswith("_test"), "refusing to reset a non-test database"
    return application


@pytest.fixture(autouse=True)
def schema(pg_app):
    from extensions import db
    pg_app.config["WTF_CSRF_ENABLED"] = False
    with pg_app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


# Named pg_app (not app) so conftest's _db_rollback wrapper stays out of the
# way: this suite resets the schema itself, and DDL would deadlock against
# that wrapper's outer transaction.

# ---------------------------------------------------------------------------
# World
# ---------------------------------------------------------------------------

def _tenant(name, n):
    from extensions import db
    from models import Company, TwilioAccount, TwilioPhoneNumber
    co = Company(name=name)
    db.session.add(co)
    db.session.flush()
    acct = TwilioAccount(company_id=co.id, from_phone=f"+1415555{n:04d}", is_active=True)
    acct.set_account_sid(f"ACsec{n}")
    acct.set_auth_token("token")
    line = TwilioPhoneNumber(company_id=co.id, phone_number=f"+1415555{n:04d}", is_active=True, sms_enabled=True,
                             campaign_sender_enabled=True, is_primary=True)
    db.session.add_all([acct, line])
    db.session.flush()
    return co, line


def _user(co, role, name):
    from extensions import db
    from models import User, UserCompanyAccess
    u = User(username=name, email=f"{name}@example.test", is_admin=False, default_company_id=co.id, password_hash="x")
    db.session.add(u)
    db.session.flush()
    db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role=role, is_default=True, is_active=True))
    return u


def _contact(co, phone, tags="vip", consent=True, **kw):
    from extensions import db
    from models import Contact
    values = dict(company_id=co.id, phone=phone, tags=tags, is_active=True,
                  sms_marketing_opt_in=consent, sms_consent_status="opted_in" if consent else None)
    values.update(kw)
    c = Contact(**values)
    db.session.add(c)
    db.session.flush()
    return c


def _segment(co, name, **kw):
    from extensions import db
    from models import Segment
    s = Segment(company_id=co.id, name=name, **kw)
    db.session.add(s)
    db.session.flush()
    return s


class Client:
    def __init__(self, app, user=None):
        self.c = app.test_client()
        if user is not None:
            with self.c.session_transaction() as sess:
                sess["_user_id"] = str(user.id)
                sess["_fresh"] = True

    def __getattr__(self, method):
        def call(*a, **kw):
            g.pop("_login_user", None)
            return getattr(self.c, method)(*a, **kw)
        return call


@pytest.fixture
def world(pg_app):
    from extensions import db
    a, line_a = _tenant("Tenant A", 1)
    b, line_b = _tenant("Tenant B", 2)
    vip = _segment(a, "vip")
    gold = _segment(a, "gold")
    empty = _segment(a, "nobody")
    contacts = [_contact(a, f"+1415444{i:04d}") for i in range(4)]
    gold_contacts = [_contact(a, f"+1415445{i:04d}", tags="gold") for i in range(2)]
    foreign_contact = _contact(b, "+14153330001")
    foreign_seg = _segment(b, "vip")
    users = {r: _user(a, r, f"{r}-a") for r in ("owner", "admin", "editor", "viewer")}
    users["outsider"] = _user(b, "owner", "owner-b")
    db.session.commit()
    return dict(a=a, b=b, line_a=line_a, line_b=line_b, vip=vip, gold=gold, empty=empty, contacts=contacts,
                gold_contacts=gold_contacts, foreign_contact=foreign_contact, foreign_seg=foreign_seg, users=users)


@pytest.fixture
def as_(pg_app, world):
    return lambda role: Client(pg_app, world["users"][role] if role else None)


@pytest.fixture
def fake_send(monkeypatch):
    from services.sms_service import SMSService
    sent = []

    def _send(cls, to, message, **kw):
        sent.append({"to": to, "from": kw.get("from_phone"), "company_id": kw.get("company_id")})
        return {"success": True, "message_sid": f"SMSEC{len(sent):04d}"}

    monkeypatch.setattr(SMSService, "send_sms", classmethod(_send))
    return sent


def _future(hours=24):
    return (datetime.utcnow() + timedelta(hours=hours)).replace(microsecond=0).isoformat()


def _draft(as_, world, segment, role="admin", **extra):
    body = {"name": "Spring", "message": "Spring sale", "segment_id": segment.id,
            "from_phone_number_id": world["line_a"].id, **extra}
    r = as_(role).post("/api/marketing/sms-campaigns", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["campaign"]["id"]


def _pending(campaign_id):
    from models import SMSRecipient
    return sorted((r.contact_id, r.phone_number) for r in SMSRecipient.query.filter_by(campaign_id=campaign_id).all())


def _events(name):
    from models import ProductEvent
    return ProductEvent.query.filter_by(event=name).all()


# ---------------------------------------------------------------------------
# Guided segments + immediate dynamic membership
# ---------------------------------------------------------------------------

def test_guided_segment_is_created_with_immediate_membership_audit_and_event(as_, world):
    from models import MarketingAuditLog, SegmentMember
    r = as_("editor").post("/api/segments", json={"template": "tag", "params": {"tag": "gold"}})
    assert r.status_code == 201, r.get_json()
    seg = r.get_json()["segment"]
    assert seg["is_dynamic"] is True and seg["member_count"] == 2 and seg["name"] == "Contacts tagged gold"
    members = {m.contact_id for m in SegmentMember.query.filter_by(segment_id=seg["id"]).all()}
    assert members == {c.id for c in world["gold_contacts"]}
    assert MarketingAuditLog.query.filter_by(action="segment_created", entity_id=seg["id"]).count() == 1
    ev = _events("segment_created")
    assert len(ev) == 1 and ev[0].company_id == world["a"].id and ev[0].properties == {
        "template": "tag", "is_dynamic": True, "members": 2}


@pytest.mark.parametrize("body,status,code", [
    ({"template": "nope"}, 422, "invalid_conditions"),
    ({"template": "tag", "params": {"tag": ""}}, 422, "invalid_conditions"),
    ({"template": "new_contacts", "params": {"days": "1.5"}}, 422, "invalid_conditions"),
    ({"name": "x", "is_dynamic": True, "conditions": [{"field": "ssn", "op": "equals", "value": 1}]}, 422,
     "invalid_conditions"),
    ({"name": "anchor", "segment_type": "contact_tag"}, 400, "system_managed_segment"),
    ({"name": "VIP"}, 409, "duplicate_name"),
])
def test_invalid_segment_definitions_fail_closed_as_structured_json(as_, world, body, status, code):
    from models import Segment
    before = Segment.query.count()
    r = as_("admin").post("/api/segments", json=body)
    assert r.status_code == status and r.is_json
    assert r.get_json()["success"] is False and r.get_json()["code"] == code
    assert Segment.query.count() == before


@pytest.mark.parametrize("role,status", [("owner", 201), ("admin", 201), ("editor", 201), ("viewer", 403)])
def test_segment_create_role_matrix(as_, world, role, status):
    r = as_(role).post("/api/segments", json={"template": "tag", "params": {"tag": "vip"}, "name": f"vip-{role}"})
    assert r.status_code == status


def test_unauthenticated_segment_and_campaign_mutations_are_refused(as_, world):
    from models import Segment, SMSCampaign
    anon = as_(None)
    for path, body in (("/api/segments", {"template": "tag", "params": {"tag": "vip"}}),
                       (f"/api/segments/{world['vip'].id}/contacts/bulk-add", {"contact_ids": [world["contacts"][0].id]}),
                       ("/api/marketing/sms-campaigns", {"message": "x", "segment_id": world["vip"].id})):
        r = anon.post(path, json=body)
        assert r.status_code in (302, 401), path
    assert anon.get(f"/api/segments/{world['vip'].id}/export.csv").status_code in (302, 401)
    assert Segment.query.count() == 4 and SMSCampaign.query.count() == 0


# ---------------------------------------------------------------------------
# Membership, tagging, export: tenant isolation
# ---------------------------------------------------------------------------

def test_single_and_bulk_membership_never_touch_foreign_contacts(as_, world):
    from models import MarketingAuditLog, SegmentMember
    vip, foreign = world["vip"], world["foreign_contact"]
    single = as_("editor").post(f"/api/segments/{vip.id}/contacts", json={"contact_id": foreign.id})
    assert single.status_code == 404 and single.get_json()["code"] == "not_found"
    bulk = as_("editor").post(f"/api/segments/{vip.id}/contacts/bulk-add",
                              json={"contact_ids": [world["contacts"][0].id, foreign.id, "junk"]})
    assert bulk.status_code == 200 and bulk.get_json()["added"] == 1 and bulk.get_json()["not_found"] == [foreign.id]
    assert SegmentMember.query.filter_by(contact_id=foreign.id).count() == 0
    removed = as_("editor").post(f"/api/segments/{vip.id}/contacts/bulk-remove",
                                 json={"contact_ids": [world["contacts"][0].id, foreign.id]})
    assert removed.get_json()["removed"] == 1 and removed.get_json()["not_found"] == [foreign.id]
    audit = MarketingAuditLog.query.filter_by(action="segment_contact_added", entity_id=vip.id).one()
    assert audit.details["contact_ids"] == [world["contacts"][0].id]
    changes = [e.properties for e in _events("segment_membership_changed")]
    assert {"action": "bulk_add", "added": 1, "skipped_excluded": 0, "not_found": 1} in changes
    assert all("contact_ids" not in p for p in changes)


def test_foreign_segment_is_not_found_for_every_segment_operation(as_, world):
    sid = world["foreign_seg"].id
    client = as_("owner")
    for method, path, body in (
            ("get", f"/api/segments/{sid}", None), ("get", f"/api/segments/{sid}/export.csv", None),
            ("post", f"/api/segments/{sid}/contacts/bulk-add", {"contact_ids": [world["contacts"][0].id]}),
            ("post", f"/api/segments/{sid}/contacts/bulk-tag", {"tag": "x", "contact_ids": [world["contacts"][0].id]}),
            ("post", f"/api/segments/{sid}/refresh", {}), ("delete", f"/api/segments/{sid}", None)):
        r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert r.status_code == 404 and r.is_json and r.get_json()["code"] == "not_found", path


def test_tagging_is_scoped_audited_and_cannot_grant_consent(as_, world):
    from extensions import db
    from models import Contact, MarketingAuditLog
    vip = world["vip"]
    ids = [c.id for c in world["contacts"][:2]] + [world["foreign_contact"].id]
    add = as_("editor").post(f"/api/segments/{vip.id}/contacts/bulk-tag", json={"action": "add", "tag": "spring", "contact_ids": ids})
    assert add.status_code == 200 and add.get_json()["changed"] == 2
    assert "spring" not in (db.session.get(Contact, world["foreign_contact"].id).tags or "")
    rm = as_("editor").post(f"/api/segments/{vip.id}/contacts/bulk-tag", json={"action": "remove", "tag": "spring", "contact_ids": ids})
    assert rm.get_json()["changed"] == 2
    consent = as_("admin").post(f"/api/segments/{vip.id}/contacts/bulk-tag",
                                json={"action": "add", "tag": "sms_opt_in", "contact_ids": ids})
    assert consent.status_code == 400 and consent.get_json()["code"] == "protected_tag"
    assert as_("viewer").post(f"/api/segments/{vip.id}/contacts/bulk-tag",
                              json={"action": "add", "tag": "x", "contact_ids": ids}).status_code == 403
    assert MarketingAuditLog.query.filter(MarketingAuditLog.action.like("segment_bulk_tag_%")).count() == 2
    assert [e.properties["action"] for e in _events("segment_tagged")] == ["add", "remove"]
    assert all("tag" not in e.properties for e in _events("segment_tagged"))


def test_export_is_tenant_scoped_role_gated_audited(as_, world):
    from models import MarketingAuditLog
    vip = world["vip"]
    assert as_("viewer").get(f"/api/segments/{vip.id}/export.csv").status_code == 403
    r = as_("editor").get(f"/api/segments/{vip.id}/export.csv")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    body = r.get_data(as_text=True)
    assert body.count("\n") == 5 and world["foreign_contact"].phone not in body
    assert MarketingAuditLog.query.filter_by(action="segment_exported", entity_id=vip.id).one().details["rows"] == 4
    assert _events("segment_exported")[0].properties == {"rows": 4, "format": "csv"}


# ---------------------------------------------------------------------------
# Campaign audiences + senders
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body,status,code", [
    ({"segment_id": "FOREIGN"}, 400, "audience_invalid"),
    ({"segment": "no such segment"}, 400, "audience_invalid"),
    ({"segment_id": "SYSTEM"}, 400, "audience_invalid"),
    ({"segment": "dup"}, 400, "audience_invalid"),
    ({"segment_id": "VIP", "from_phone_number_id": "FOREIGN_LINE"}, 403, "sender_not_authorized"),
])
def test_campaign_rejects_foreign_missing_system_ambiguous_audiences_and_foreign_senders(as_, world, body, status, code):
    from models import SMSCampaign
    system = _segment(world["a"], "anchor", segment_type="contact_tag")
    _segment(world["a"], "dup"); _segment(world["a"], "DUP")
    from extensions import db
    db.session.commit()
    sub = {"FOREIGN": world["foreign_seg"].id, "SYSTEM": system.id, "VIP": world["vip"].id,
           "FOREIGN_LINE": world["line_b"].id}
    payload = {"name": "x", "message": "hello", **{k: sub.get(v, v) for k, v in body.items()}}
    r = as_("admin").post("/api/marketing/sms-campaigns", json=payload)
    assert r.status_code == status and r.is_json, r.get_data(as_text=True)
    assert r.get_json()["code"] == code
    assert SMSCampaign.query.count() == 0
    assert _events("sms_campaign_failed")[-1].properties["code"] == code


def test_campaign_from_segment_records_draft_event_and_role_matrix(as_, world):
    for role, status in (("owner", 201), ("admin", 201), ("editor", 201), ("viewer", 403)):
        r = as_(role).post("/api/marketing/sms-campaigns",
                           json={"name": role, "message": "hello", "segment_id": world["vip"].id})
        assert r.status_code == status, role
    assert len(_events("sms_campaign_draft_created")) == 3
    cid = _draft(as_, world, world["vip"])
    assert as_("editor").post(f"/api/marketing/sms-campaigns/{cid}/schedule",
                              json={"scheduled_at": _future()}).status_code == 403
    outsider = as_("outsider")
    assert outsider.get(f"/api/marketing/sms-campaigns/{cid}").status_code == 404
    assert outsider.put(f"/api/marketing/sms-campaigns/{cid}", json={"message": "pwn"}).status_code == 404
    assert outsider.post(f"/api/marketing/sms-campaigns/{cid}/schedule", json={}).status_code == 404


def test_scheduling_requires_an_active_authorized_tenant_sender(as_, world, fake_send):
    from extensions import db
    from models import SMSCampaign
    cid = _draft(as_, world, world["vip"], from_phone_number_id=None)
    # No sender chosen and no primary campaign line to default to: refused.
    world["line_a"].is_primary = False
    db.session.commit()
    no_sender = as_("admin").post(f"/api/marketing/sms-campaigns/{cid}/schedule", json={"scheduled_at": _future()})
    assert no_sender.status_code == 409 and no_sender.get_json()["code"] == "sender_required"
    foreign = as_("admin").post(f"/api/marketing/sms-campaigns/{cid}/schedule",
                                json={"scheduled_at": _future(), "from_phone_number_id": world["line_b"].id})
    assert foreign.status_code == 403 and foreign.get_json()["code"] == "sender_not_authorized"
    world["line_a"].is_active = False
    db.session.commit()
    inactive = as_("admin").post(f"/api/marketing/sms-campaigns/{cid}/schedule",
                                 json={"scheduled_at": _future(), "from_phone_number_id": world["line_a"].id})
    assert inactive.status_code == 403
    assert db.session.get(SMSCampaign, cid).status == "draft" and _pending(cid) == []
    assert fake_send == []


# ---------------------------------------------------------------------------
# Eligibility precedence, phones, idempotency, editing, rollback
# ---------------------------------------------------------------------------

def test_stop_suppression_consent_invalid_and_duplicate_phones_are_excluded_and_audited(as_, world, fake_send):
    from extensions import db
    from models import MarketingAuditLog
    a, mix = world["a"], _segment(world["a"], "mix")
    ok = _contact(a, "+14154460001", tags="mix")
    dup = _contact(a, "(415) 446-0001", tags="mix")  # same canonical number as ok
    stop = _contact(a, "+14154460002", tags="mix", sms_opted_out=True)
    dns = _contact(a, "+14154460003", tags="mix", do_not_sms=True)
    dnm = _contact(a, "+14154460004", tags="mix", do_not_market=True)
    archived = _contact(a, "+14154460005", tags="mix", is_active=False)
    no_consent = _contact(a, "+14154460006", tags="mix", consent=False)
    invalid = _contact(a, "12", tags="mix")
    db.session.commit()
    cid = _draft(as_, world, mix)
    r = as_("admin").post(f"/api/marketing/sms-campaigns/{cid}/schedule", json={"scheduled_at": _future()})
    assert r.status_code == 200, r.get_json()
    aud = r.get_json()["audience"]
    assert aud["eligible_recipients"] == 1 and aud["duplicate_phone_numbers"] == 1 and aud["invalid_phone_numbers"] == 1
    assert _pending(cid) == [(ok.id, "+14154460001")]
    reasons = {a_.details["reason"]: a_.details["contact_ids"] for a_ in MarketingAuditLog.query.filter_by(
        action="campaign_contact_skipped_suppression", entity_id=cid).all()}
    assert reasons["sms_opted_out"] == [stop.id] and reasons["do_not_sms"] == [dns.id]
    assert reasons["do_not_market"] == [dnm.id] and reasons["missing_consent"] == [no_consent.id]
    assert reasons["invalid_phone"] == [invalid.id] and reasons["duplicate_phone"] == [dup.id]
    assert reasons["archived_or_suppressed"] == [archived.id]
    # STOP wins at send time even after scheduling.
    db.session.get(type(ok), ok.id).sms_opted_out = True
    db.session.commit()
    as_("admin").post(f"/api/marketing/sms-campaigns/{cid}/send", json={})
    assert fake_send == []


def test_missing_promotional_consent_means_nothing_is_scheduled(as_, world):
    from extensions import db
    from models import SMSCampaign
    seg = _segment(world["a"], "noconsent")
    _contact(world["a"], "+14154470001", tags="noconsent", consent=False)
    db.session.commit()
    cid = _draft(as_, world, seg)
    r = as_("admin").post(f"/api/marketing/sms-campaigns/{cid}/schedule", json={"scheduled_at": _future()})
    assert r.status_code == 400 and r.get_json()["code"] == "no_eligible_recipients"
    assert r.get_json()["counts"]["missing_sms_consent"] == 1
    assert db.session.get(SMSCampaign, cid).status == "draft" and _pending(cid) == []
    assert _events("sms_campaign_failed")[-1].properties["code"] == "no_eligible_recipients"


def test_recipient_rebuild_is_idempotent(as_, world):
    from extensions import db
    from models import SMSCampaign
    from services.sms_campaign_workflow import rebuild_pending
    cid = _draft(as_, world, world["vip"])
    first = as_("admin").post(f"/api/marketing/sms-campaigns/{cid}/schedule", json={"scheduled_at": _future()})
    snapshot = _pending(cid)
    assert first.status_code == 200 and len(snapshot) == 4
    again = as_("admin").post(f"/api/marketing/sms-campaigns/{cid}/schedule", json={"scheduled_at": _future(30)})
    assert again.status_code == 200 and _pending(cid) == snapshot
    campaign = db.session.get(SMSCampaign, cid)
    rebuild_pending(campaign); rebuild_pending(campaign)
    db.session.commit()
    assert _pending(cid) == snapshot


def test_scheduled_campaign_audience_edit_rebuilds_pending_and_locks_after_send_starts(as_, world, fake_send):
    from extensions import db
    from models import SMSRecipient
    cid = _draft(as_, world, world["vip"])
    as_("admin").post(f"/api/marketing/sms-campaigns/{cid}/schedule", json={"scheduled_at": _future()})
    # An editor may edit drafts, but re-validating a scheduled campaign's sender
    # uses the editor's own line access -- without it the edit is refused.
    denied = as_("editor").put(f"/api/marketing/sms-campaigns/{cid}", json={"segment_id": world["gold"].id})
    assert denied.status_code == 403 and denied.get_json()["code"] == "sender_required"
    assert {c for c, _ in _pending(cid)} == {c.id for c in world["contacts"]}
    r = as_("admin").put(f"/api/marketing/sms-campaigns/{cid}", json={"segment_id": world["gold"].id})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["campaign"]["status"] == "scheduled"
    assert {c for c, _ in _pending(cid)} == {c.id for c in world["gold_contacts"]}
    ev = _events("sms_campaign_audience_changed")[-1].properties
    assert ev == {"status": "scheduled", "rebuilt": True, "eligible": 2}
    row = SMSRecipient.query.filter_by(campaign_id=cid).first()
    row.status = "sent"; row.attempted_at = datetime.utcnow()
    db.session.commit()
    locked = as_("admin").put(f"/api/marketing/sms-campaigns/{cid}", json={"segment_id": world["vip"].id})
    assert locked.status_code == 409 and locked.get_json()["code"] == "campaign_locked"
    assert {c for c, _ in _pending(cid)} == {c.id for c in world["gold_contacts"]}


def test_failed_audience_rebuild_rolls_back_everything(as_, world):
    from extensions import db
    from models import MarketingAuditLog, SMSCampaign
    cid = _draft(as_, world, world["vip"])
    as_("admin").post(f"/api/marketing/sms-campaigns/{cid}/schedule", json={"scheduled_at": _future()})
    before = _pending(cid)
    audits = MarketingAuditLog.query.count()
    r = as_("admin").put(f"/api/marketing/sms-campaigns/{cid}", json={"segment_id": world["empty"].id, "name": "renamed"})
    assert r.status_code == 400 and r.get_json()["code"] == "no_eligible_recipients"
    db.session.expire_all()
    c = db.session.get(SMSCampaign, cid)
    assert c.name == "Spring" and c.selected_tag_ids == [world["vip"].id] and c.status == "scheduled"
    assert _pending(cid) == before
    assert MarketingAuditLog.query.count() == audits
    assert not _events("sms_campaign_audience_changed")


# ---------------------------------------------------------------------------
# Promotional opt-in audience errors stay structured JSON
# ---------------------------------------------------------------------------

def test_promotional_optin_audience_errors_are_structured_json(as_, world):
    from extensions import db
    system = _segment(world["a"], "automation", segment_type="automation_rule")
    db.session.commit()
    ok = as_("editor").get(f"/api/promotional-optin/audience?segment_id={world['vip'].id}")
    assert ok.status_code == 200 and ok.get_json()["success"] is True
    for role, sid, status in (("editor", world["foreign_seg"].id, 404), ("viewer", world["vip"].id, 403),
                              ("editor", system.id, 422), ("editor", 999999, 404)):
        r = as_(role).get(f"/api/promotional-optin/audience?segment_id={sid}")
        assert r.status_code == status and r.is_json, (role, sid, r.get_data(as_text=True)[:200])
        assert r.get_json()["success"] is False and r.get_json()["error"]


# ---------------------------------------------------------------------------
# CSRF on browser mutations
# ---------------------------------------------------------------------------

def test_browser_mutations_require_csrf(pg_app, as_, world):
    from models import Segment, SMSCampaign
    pg_app.config["WTF_CSRF_ENABLED"] = True
    client = as_("admin")
    for path, body in (("/api/segments", {"template": "tag", "params": {"tag": "vip"}}),
                       (f"/api/segments/{world['vip'].id}/contacts/bulk-tag",
                        {"tag": "x", "contact_ids": [world["contacts"][0].id]}),
                       ("/api/marketing/sms-campaigns", {"message": "hi", "segment_id": world["vip"].id})):
        r = client.post(path, json=body)
        assert r.status_code == 400 and r.get_json()["code"] == "csrf_failed", path
    assert Segment.query.count() == 4 and SMSCampaign.query.count() == 0
    raw = "gtm-security-csrf"
    with client.c.session_transaction() as sess:
        sess["csrf_token"] = raw
    signed = URLSafeTimedSerializer(pg_app.secret_key, salt="wtf-csrf-token").dumps(raw)
    ok = client.post("/api/segments", data=json.dumps({"template": "tag", "params": {"tag": "vip"}, "name": "csrf ok"}),
                     content_type="application/json", headers={"X-CSRFToken": signed})
    assert ok.status_code == 201


def test_product_events_carry_no_contact_data(as_, world):
    from models import ProductEvent
    _draft(as_, world, world["vip"])
    as_("editor").post(f"/api/segments/{world['vip'].id}/contacts/bulk-tag",
                       json={"action": "add", "tag": "secret-tag", "contact_ids": [world["contacts"][0].id]})
    blob = json.dumps([e.properties for e in ProductEvent.query.all()])
    for c in world["contacts"]:
        assert c.phone not in blob
    assert "secret-tag" not in blob and "Spring" not in blob
