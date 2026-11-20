"""Segment membership actions: exclusion precedence in the canonical SMS
resolver, bulk add/tag/export, restore, roles, tenant isolation."""
import csv
import io
import os

import pytest
from flask import g

from app import create_app
from extensions import db
from models import Company, Contact, MarketingAuditLog, Segment, SegmentMember, SMSCampaign, User, UserCompanyAccess
from services.contact_audience import resolve_sms_campaign_recipients


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


class Client:
    def __init__(self, app, user):
        self.c = app.test_client()
        with self.c.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True

    def __getattr__(self, method):
        def call(*a, **kw):
            g.pop("_login_user", None)
            return getattr(self.c, method)(*a, **kw)
        return call


def _user(co, role, name):
    u = User(username=name, email=f"{name}@example.test", is_admin=False, default_company_id=co.id, password_hash="x")
    db.session.add(u)
    db.session.flush()
    db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role=role, is_default=True, is_active=True))
    return u


@pytest.fixture
def world(app):
    co, other = Company(name="Seg A"), Company(name="Seg B")
    db.session.add_all([co, other])
    db.session.flush()
    seg = Segment(company_id=co.id, name="vip")
    foreign_seg = Segment(company_id=other.id, name="vip")
    db.session.add_all([seg, foreign_seg])
    db.session.flush()
    contacts = [Contact(company_id=co.id, first_name=f"C{i}", phone=f"+1415777{i:04d}", tags="vip", is_active=True,
                        sms_marketing_opt_in=True, sms_consent_status="opted_in") for i in range(4)]
    foreign = Contact(company_id=other.id, first_name="=HYPERLINK(\"x\")", phone="+14157779999", tags="vip",
                      sms_marketing_opt_in=True, sms_consent_status="opted_in")
    db.session.add_all(contacts + [foreign])
    db.session.flush()
    for c in contacts:
        db.session.add(SegmentMember(segment_id=seg.id, contact_id=c.id, source="manual"))
    users = {"editor": _user(co, "editor", "seg-editor"), "viewer": _user(co, "viewer", "seg-viewer"),
             "outsider": _user(other, "admin", "seg-outsider")}
    db.session.commit()
    return dict(co=co, other=other, seg=seg, foreign_seg=foreign_seg, contacts=contacts, foreign=foreign, users=users)


def _audience_ids(world):
    probe = SMSCampaign(company_id=world["co"].id, segment="vip", selected_tag_ids=[world["seg"].id],
                        audience_filter={"selected_tag_ids": [world["seg"].id]})
    return {c.id for c, _ in resolve_sms_campaign_recipients(probe)["recipients"]}


def test_removed_and_excluded_members_leave_the_sms_audience_even_with_matching_tag(app, world):
    editor = Client(app, world["users"]["editor"])
    a, b, c, d = world["contacts"]
    assert _audience_ids(world) == {a.id, b.id, c.id, d.id}
    assert editor.post(f"/api/segments/{world['seg'].id}/contacts/bulk-remove", json={"contact_ids": [a.id]}).status_code == 200
    assert editor.post(f"/api/segments/{world['seg'].id}/contacts/{b.id}/exclude", json={}).status_code == 200
    # Both still carry the "vip" tag text; explicit decisions must win.
    assert _audience_ids(world) == {c.id, d.id}
    # Restore brings the excluded contact back, audited.
    assert editor.post(f"/api/segments/{world['seg'].id}/contacts/{b.id}/restore").status_code == 200
    assert _audience_ids(world) == {b.id, c.id, d.id}
    actions = {r.action for r in MarketingAuditLog.query.filter_by(entity_id=world["seg"].id)}
    assert {"segment_contact_removed", "segment_contact_excluded", "segment_contact_restored"} <= actions


def test_bulk_add_never_undoes_a_permanent_exclusion_and_ignores_foreign_contacts(app, world):
    editor = Client(app, world["users"]["editor"])
    a = world["contacts"][0]
    editor.post(f"/api/segments/{world['seg'].id}/contacts/{a.id}/exclude", json={})
    r = editor.post(f"/api/segments/{world['seg'].id}/contacts/bulk-add",
                    json={"contact_ids": [a.id, world["foreign"].id]})
    body = r.get_json()
    assert r.status_code == 200
    assert body["added"] == 0 and body["skipped_excluded"] == [a.id] and body["not_found"] == [world["foreign"].id]
    assert SegmentMember.query.filter_by(segment_id=world["seg"].id, contact_id=a.id).one().is_excluded is True
    assert SegmentMember.query.filter_by(contact_id=world["foreign"].id).count() == 0
    single = editor.post(f"/api/segments/{world['seg'].id}/contacts", json={"contact_id": a.id})
    assert single.status_code == 409 and single.get_json()["code"] == "contact_excluded"


def test_bulk_tag_add_remove_and_protected_consent_tags(app, world):
    editor = Client(app, world["users"]["editor"])
    ids = [c.id for c in world["contacts"][:2]]
    r = editor.post(f"/api/segments/{world['seg'].id}/contacts/bulk-tag", json={"contact_ids": ids, "tag": "spring", "action": "add"})
    assert r.status_code == 200 and r.get_json()["changed"] == 2
    assert all("spring" in db.session.get(Contact, i).tags for i in ids)
    r = editor.post(f"/api/segments/{world['seg'].id}/contacts/bulk-tag", json={"contact_ids": ids, "tag": "SPRING", "action": "remove"})
    assert r.status_code == 200 and r.get_json()["changed"] == 2
    assert all("spring" not in db.session.get(Contact, i).tags.lower() for i in ids)
    for tag in ("sms_consent", "SMS Opt Out", "sms_opt_out", "do_not_contact", "a,b"):
        r = editor.post(f"/api/segments/{world['seg'].id}/contacts/bulk-tag", json={"contact_ids": ids, "tag": tag, "action": "add"})
        assert r.status_code == 400, tag
    # Foreign contact ids are ignored, never tagged.
    r = editor.post(f"/api/segments/{world['seg'].id}/contacts/bulk-tag", json={"contact_ids": [world["foreign"].id], "tag": "x"})
    assert r.get_json()["not_found"] == [world["foreign"].id]
    assert "x" not in (db.session.get(Contact, world["foreign"].id).tags or "")


def test_export_csv_is_tenant_scoped_role_gated_audited_and_injection_safe(app, world):
    editor = Client(app, world["users"]["editor"])
    a = world["contacts"][0]
    a.first_name = "=cmd|' /C calc'!A0"
    editor.post(f"/api/segments/{world['seg'].id}/contacts/{world['contacts'][1].id}/exclude", json={})
    db.session.commit()
    r = editor.get(f"/api/segments/{world['seg'].id}/export.csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    rows = list(csv.DictReader(io.StringIO(r.get_data(as_text=True))))
    ids = {int(row["contact_id"]) for row in rows}
    assert ids == {world["contacts"][0].id, world["contacts"][2].id, world["contacts"][3].id}
    assert world["foreign"].id not in ids
    first = next(row for row in rows if int(row["contact_id"]) == a.id)
    assert first["first_name"].startswith("'=")
    assert first["phone"] == a.phone  # E.164 left readable
    assert MarketingAuditLog.query.filter_by(action="segment_exported", entity_id=world["seg"].id).count() == 1
    assert Client(app, world["users"]["viewer"]).get(f"/api/segments/{world['seg'].id}/export.csv").status_code == 403
    assert Client(app, world["users"]["outsider"]).get(f"/api/segments/{world['seg'].id}/export.csv").status_code == 404


def test_viewer_and_outsider_cannot_mutate_membership(app, world):
    sid = world["seg"].id
    cid = world["contacts"][0].id
    for role, expected in (("viewer", 403), ("outsider", 404)):
        c = Client(app, world["users"][role])
        assert c.post(f"/api/segments/{sid}/contacts/bulk-add", json={"contact_ids": [cid]}).status_code == expected
        assert c.post(f"/api/segments/{sid}/contacts/bulk-remove", json={"contact_ids": [cid]}).status_code == expected
        assert c.post(f"/api/segments/{sid}/contacts/bulk-tag", json={"contact_ids": [cid], "tag": "x"}).status_code == expected
        assert c.post(f"/api/segments/{sid}/contacts/{cid}/exclude", json={}).status_code == expected
        assert c.post(f"/api/segments/{sid}/contacts/{cid}/restore").status_code == expected
    assert SegmentMember.query.filter_by(segment_id=sid, contact_id=cid).one().removed_at is None


def test_segment_detail_page_has_no_coming_soon_and_readable_rules(app, world):
    editor = Client(app, world["users"]["editor"])
    world["seg"].is_dynamic = True
    world["seg"].conditions = [{"field": "tag", "op": "contains", "value": "vip"}]
    db.session.commit()
    r = editor.get(f"/segments/{world['seg'].id}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Coming soon" not in html
    assert "Tag contains" in html
    assert f"segment_id={world['seg'].id}" in html
    assert "export.csv" in html
