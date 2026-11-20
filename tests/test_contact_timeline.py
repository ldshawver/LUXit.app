"""Unified relationship timeline (M1): canonical identity, every source, tenant isolation."""
import uuid
from datetime import datetime, timedelta

import pytest
from flask import g
from flask.testing import FlaskClient

from app import create_app
from extensions import db
from models import (
    Company, Contact, ContactActivity, ContactPhoneNumber, ContactSourceEvent, ContactTask, Deal, DealActivity,
    Opportunity, TouchpointEvent, TwilioCallLog, TwilioConversation, TwilioMessage, User, UserCompanyAccess,
    VoiceVoicemailMessage,
)
from services.contact_timeline import build_timeline

PHONE = "+19168675311"
ALT_PHONE = "+19168675312"


class _FreshUserClient(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@pytest.fixture
def env():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    app.test_client_class = _FreshUserClient
    with app.app_context():
        db.create_all()
        tag = uuid.uuid4().hex[:8]
        a, b = Company(name=f"TL A {tag}"), Company(name=f"TL B {tag}")
        db.session.add_all([a, b])
        db.session.flush()
        t0 = datetime(2026, 1, 1, 12, 0, 0)

        main = Contact(company_id=a.id, first_name="Robin", phone=PHONE, email=f"robin-{tag}@example.com")
        db.session.add(main)
        db.session.flush()
        dup = Contact(company_id=a.id, first_name="Robin dup", phone=ALT_PHONE, merged_into_contact_id=main.id,
                      is_active=False)
        other = Contact(company_id=b.id, first_name="Robin B", phone=PHONE)
        db.session.add_all([dup, other])
        db.session.flush()
        db.session.add(ContactPhoneNumber(company_id=a.id, contact_id=main.id, original_value=PHONE,
                                          normalized_value=PHONE, is_primary=True))

        conv = TwilioConversation(company_id=a.id, contact_id=main.id, from_number=PHONE, to_number="+19165550100")
        conv_dup = TwilioConversation(company_id=a.id, contact_id=dup.id, from_number=ALT_PHONE, to_number="+19165550100")
        conv_b = TwilioConversation(company_id=b.id, contact_id=other.id, from_number=PHONE, to_number="+19165550199")
        db.session.add_all([conv, conv_dup, conv_b])
        db.session.flush()
        u = User(username=f"tl-{tag}", email=f"tl-{tag}@example.com", password_hash="x", default_company_id=a.id)
        ub = User(username=f"tlb-{tag}", email=f"tlb-{tag}@example.com", password_hash="x", default_company_id=b.id)
        db.session.add_all([u, ub])
        db.session.flush()
        db.session.add_all([
            UserCompanyAccess(user_id=u.id, company_id=a.id, role="editor", is_default=True),
            UserCompanyAccess(user_id=ub.id, company_id=b.id, role="editor", is_default=True),
        ])
        deal = Deal(company_id=a.id, contact_id=main.id, name="Kitchen remodel", stage="proposal")
        db.session.add(deal)
        db.session.flush()
        db.session.add_all([
            TwilioMessage(conversation_id=conv.id, company_id=a.id, direction="inbound", body="Hi, can you quote?",
                          created_at=t0),
            TwilioMessage(conversation_id=conv_dup.id, company_id=a.id, direction="outbound", body="From the duplicate",
                          created_at=t0 + timedelta(minutes=1)),
            TwilioMessage(conversation_id=conv_b.id, company_id=b.id, direction="inbound", body="TENANT B SECRET",
                          created_at=t0 + timedelta(minutes=2)),
            TwilioCallLog(company_id=a.id, contact_id=None, direction="inbound", from_number=PHONE, to_number="+19165550100",
                          status="no-answer", created_at=t0 + timedelta(minutes=3)),
            TwilioCallLog(company_id=b.id, contact_id=None, direction="inbound", from_number=PHONE, to_number="+19165550199",
                          status="completed", created_at=t0 + timedelta(minutes=3)),
            VoiceVoicemailMessage(company_id=a.id, from_number=PHONE, to_number="+19165550100",
                                  transcription_text="Call me back about the quote", created_at=t0 + timedelta(minutes=4)),
            VoiceVoicemailMessage(company_id=b.id, from_number=PHONE, to_number="+19165550199",
                                  transcription_text="TENANT B VOICEMAIL", created_at=t0 + timedelta(minutes=4)),
            ContactActivity(company_id=a.id, contact_id=main.id, activity_type="note", title="Met at expo",
                            description="Interested in Q2", created_at=t0 + timedelta(minutes=5)),
            ContactTask(company_id=a.id, contact_id=main.id, title="Send quote", created_at=t0 + timedelta(minutes=6)),
            Opportunity(company_id=a.id, contact_id=main.id, name="Remodel", created_at=t0 + timedelta(minutes=7)),
            DealActivity(deal_id=deal.id, activity_type="stage_change", description="lead → proposal",
                         activity_date=t0 + timedelta(minutes=8)),
            TouchpointEvent(company_id=a.id, contact_id=main.id, touchpoint_type="page_view",
                            page_url="https://example.com/pricing", created_at=t0 + timedelta(minutes=9)),
            ContactSourceEvent(company_id=a.id, contact_id=main.id, source="web_form", event_type="created",
                               event_at=t0 - timedelta(days=1)),
        ])
        db.session.commit()
        yield app, {"a": a.id, "b": b.id, "main": main.id, "dup": dup.id, "other": other.id, "user": u.id, "user_b": ub.id}
        db.session.remove()


def _as(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return c


def test_timeline_merges_every_source_newest_first(env):
    app, ids = env
    tl = build_timeline(ids["a"], ids["main"])
    kinds = [e["kind"] for e in tl["entries"]]
    assert kinds == ["touchpoint", "deal", "opportunity", "task", "activity", "voicemail", "call",
                     "message", "message", "source"]
    assert tl["canonical_contact_id"] == ids["main"] and tl["merged_contact_ids"] == [ids["dup"]]
    assert PHONE in tl["identifiers"]["phones"] and ALT_PHONE in tl["identifiers"]["phones"]
    for e in tl["entries"]:
        assert e["source"]["table"] and e["source"]["id"] and e["at"]


def test_merged_contact_resolves_to_canonical_and_marks_provenance(env):
    app, ids = env
    tl = build_timeline(ids["a"], ids["dup"])
    assert tl["canonical_contact_id"] == ids["main"]
    dup_msg = [e for e in tl["entries"] if e["summary"] == "From the duplicate"]
    assert dup_msg and dup_msg[0]["merged_from"] == ids["dup"]
    call = next(e for e in tl["entries"] if e["kind"] == "call")
    assert call["title"] == "Missed call" and call["provenance"] == "phone_match"


def test_other_tenant_rows_never_appear(env):
    app, ids = env
    blob = repr(build_timeline(ids["a"], ids["main"]))
    assert "TENANT B" not in blob
    assert build_timeline(ids["b"], ids["main"]) is None
    assert build_timeline(ids["a"], ids["other"]) is None
    assert build_timeline(None, ids["main"]) is None


def test_paging_and_kind_filter(env):
    app, ids = env
    first = build_timeline(ids["a"], ids["main"], limit=4)
    assert len(first["entries"]) == 4 and first["next_before"]
    from services.contact_timeline import parse_before
    second = build_timeline(ids["a"], ids["main"], limit=50, before=parse_before(first["next_before"]))
    seen = {e["id"] for e in first["entries"]}
    assert seen.isdisjoint({e["id"] for e in second["entries"]})
    assert len(seen) + len(second["entries"]) == 10
    only = build_timeline(ids["a"], ids["main"], kinds=["voicemail", "call"])
    assert {e["kind"] for e in only["entries"]} == {"voicemail", "call"}


def test_timeline_api_is_tenant_scoped(env):
    app, ids = env
    r = _as(app, ids["user"]).get(f"/api/contacts/{ids['main']}/timeline?limit=3")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] and len(body["entries"]) == 3
    assert _as(app, ids["user_b"]).get(f"/api/contacts/{ids['main']}/timeline").status_code == 404
    assert _as(app, ids["user"]).get(f"/api/contacts/{ids['main']}/timeline?kinds=bogus").get_json()["entries"] == []


def test_logged_activity_keeps_subject_and_details(env):
    app, ids = env
    c = _as(app, ids["user"])
    r = c.post(f"/api/contacts/{ids['main']}/activities/add",
               data={"activity_type": "call", "subject": "Discovery call", "description": "Budget 20k",
                     "outcome": "interested", "duration_minutes": "15"})
    assert r.get_json()["success"]
    listed = c.get(f"/api/contacts/{ids['main']}/activities").get_json()["activities"]
    row = next(a for a in listed if a["subject"] == "Discovery call")
    assert row["outcome"] == "interested" and row["duration_minutes"] == 15 and row["type"] == "call"
    tl = build_timeline(ids["a"], ids["main"], kinds=["activity"])
    assert any(e["title"] == "Discovery call" and e["actor_user_id"] == ids["user"] for e in tl["entries"])


def test_activities_page_renders_timeline_panel(env):
    app, ids = env
    r = _as(app, ids["user"]).get(f"/contacts/{ids['main']}/activities")
    assert r.status_code == 200
    assert b"Relationship timeline" in r.data and f"/api/contacts/{ids['main']}/timeline".encode() in r.data
