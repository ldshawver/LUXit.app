"""RC 668fd9e — ONE consolidated release acceptance pass.

Covers the §2 acceptance matrix for the release: A auth, B Phone Availability,
C promotional opt-in (12-step + generic-YES), D campaign resolver
preview==execution for every purpose, E admin/security on the newly affected
resources. Runs against the exact RC code deployed to staging.
"""
import os
from datetime import datetime
from types import SimpleNamespace

import pytest
from werkzeug.security import generate_password_hash

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "g2CDXwdc6VKAElQ5QWqFBCsmXL_dQAs3e44_Gl1oJaU=")

from app import create_app
from extensions import db
from models import (
    Company, Contact, PromotionalConsentEvent, PromotionalOptInSolicitation,
    PWADevice, Segment, SegmentMember, TwilioAccount, TwilioConversation,
    TwilioMessage, User, UserCompanyAccess, user_company,
)
from services.phone_availability import is_available, set_availability


@pytest.fixture
def app():
    a = create_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    with a.app_context():
        db.create_all()
        yield a
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _co(name):
    c = Company(name=name, is_active=True)
    db.session.add(c); db.session.flush()
    return c


def _user(co, name, *, role="staff", manage=False, is_admin=False, password="pw"):
    u = User(username=name, email=f"{name}@x.com",
             password_hash=generate_password_hash(password),
             default_company_id=co.id, is_admin=is_admin)
    db.session.add(u); db.session.flush()
    db.session.add(UserCompanyAccess(user_id=u.id, company_id=co.id, role=role,
                                     is_default=True, is_active=True,
                                     pwa_access_enabled=True, can_access_full_app=True,
                                     manage_users_enabled=manage))
    db.session.execute(user_company.insert().values(user_id=u.id, company_id=co.id, is_default=True))
    db.session.flush()
    return u


def _login(client, uid):
    with client.session_transaction() as s:
        s.clear()
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        s["user_id"] = uid
        s["logged_in"] = True


def _logout(client):
    with client.session_transaction() as s:
        s.clear()


def _twilio(co, from_phone="+15559999999"):
    acc = TwilioAccount(company_id=co.id, from_phone=from_phone, is_active=True)
    acc.set_account_sid("ACtest"); acc.set_auth_token("tok")
    db.session.add(acc); db.session.flush()
    return acc


def _contact(co, phone, **kw):
    c = Contact(company_id=co.id, phone=phone, normalized_phone=phone, is_active=True, **kw)
    db.session.add(c); db.session.flush()
    return c


def _evidence(co, phone, sid):
    conv = TwilioConversation(company_id=co.id, from_number=phone, to_number="+15559999999")
    db.session.add(conv); db.session.flush()
    db.session.add(TwilioMessage(company_id=co.id, conversation_id=conv.id, direction="inbound",
                                 twilio_sid=sid, from_number=phone, body="an order please"))
    db.session.flush()


def _segment(co, contact_ids, name="My Order Customer"):
    seg = Segment(company_id=co.id, name=name, segment_type="custom", match_mode="any", is_active=True)
    db.session.add(seg); db.session.flush()
    for cid in contact_ids:
        db.session.add(SegmentMember(segment_id=seg.id, contact_id=cid, source="manual", is_excluded=False))
    db.session.flush()
    return seg


def _campaign(co, seg, purpose):
    return SimpleNamespace(company_id=co.id, id=None, segment=None,
                           audience_filter={"selected_tag_ids": [seg.id], "campaign_purpose": purpose})


# =====================================================================
# A. AUTH
# =====================================================================

def test_A_auth_login_tenant_navigation_logout_relogin(client, app):
    with app.app_context():
        co = _co("Auth Tenant")
        other = _co("Other Tenant")
        u = _user(co, "acceptuser", role="admin", manage=True, password="Sekret-Pw-1")
        uid, cid, other_id = u.id, co.id, other.id
        db.session.commit()

    # login
    r = client.post("/auth/login", data={"email": "acceptuser@x.com", "password": "Sekret-Pw-1"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    # correct tenant + dashboard loads + authenticated navigation
    with client.session_transaction() as s:
        assert s.get("_user_id") == str(uid)
    dash = client.get("/dashboard")
    assert dash.status_code == 200
    for path in ("/contacts", "/segments", "/sms/campaigns"):
        assert client.get(path).status_code in (200, 302), path
    # the authenticated user resolves to its own tenant, not the other one
    with app.app_context():
        assert db.session.get(User, uid).default_company_id == cid
        assert cid != other_id
    # logout
    out = client.get("/auth/logout", follow_redirects=False)
    assert out.status_code in (302, 303)
    assert client.get("/dashboard").status_code in (302, 303)  # unauth -> redirect
    # login again
    r2 = client.post("/auth/login", data={"email": "acceptuser@x.com", "password": "Sekret-Pw-1"}, follow_redirects=False)
    assert r2.status_code in (302, 303)
    assert client.get("/dashboard").status_code == 200


# =====================================================================
# B. PHONE AVAILABILITY
# =====================================================================

def test_B_phone_availability_matrix(client, app):
    import inbox_pwa
    from unittest.mock import patch
    from models import PhoneNumberUserPermission, TwilioPhoneNumber
    with app.app_context():
        co = _co("PA Tenant")
        other = _co("PA Other")
        acct = _twilio(co, "+19165980000")
        line = TwilioPhoneNumber(company_id=co.id, twilio_account_id=acct.id, phone_number="+19165980000",
                                 friendly_name="Shared", voice_enabled=True, is_active=True,
                                 during_hours_route="ring_pwa", after_hours_route="voicemail",
                                 voicemail_greeting_text="msg")
        db.session.add(line); db.session.flush()
        A = _user(co, "pa_a", role="owner")
        B = _user(co, "pa_b", role="staff")
        plain = _user(co, "pa_plain", role="viewer")
        admin = _user(co, "pa_admin", role="admin", manage=True)
        outsider = _user(other, "pa_outsider", role="admin", manage=True)
        for u in (A, B, admin):
            db.session.add(PWADevice(company_id=co.id, user_id=u.id, device_key=f"d-{u.id}",
                                     approved_status="approved", lifecycle_status="active"))
            db.session.add(PhoneNumberUserPermission(company_id=co.id, user_id=u.id, phone_number_id=line.id,
                                                     can_access_pwa=True, can_view_sms=True, can_view_calls=True,
                                                     can_view_voicemail=True, can_call=True))
        db.session.commit()
        ids = dict(co=co.id, A=A.id, B=B.id, plain=plain.id, admin=admin.id, outsider=outsider.id, other=other.id)

    with app.app_context():
        set_availability(ids["B"], ids["co"], "away", actor_user_id=ids["B"], source="user")
        db.session.commit()
        # Available receives eligibility, Away does not
        assert is_available(ids["A"], ids["co"]) is True
        assert is_available(ids["B"], ids["co"]) is False
        from services.phone_availability import available_user_ids
        elig = set(available_user_ids(ids["co"]))
        assert ids["A"] in elig and ids["B"] not in elig

    # Away user gets no shared-line push / badge; Available does
    with app.app_context():
        conv = SimpleNamespace(id=99, contact_name="C", from_number="+14155551212",
                               phone_number_id=None, last_message_at=None, last_message_preview="")
        with patch.object(inbox_pwa, "send_pwa_push_notification") as push:
            inbox_pwa._fire_push_notification(ids["co"], conv, "hi")
        pushed = set(push.call_args.kwargs.get("user_ids", []))
        assert ids["B"] not in pushed
        assert ids["A"] in pushed

    # SMS still persists while Away (the tenant TwilioAccount was created in setup)
    resp = client.post("/twilio/sms/inbound", data={"From": "+14155551212", "To": "+19165980000",
                                                    "Body": "still here", "MessageSid": "PASMS1"})
    assert resp.status_code == 200
    with app.app_context():
        assert TwilioMessage.query.filter_by(twilio_sid="PASMS1").count() == 1

    # Away -> Available restores eligibility
    with app.app_context():
        set_availability(ids["B"], ids["co"], "available", actor_user_id=ids["B"], source="user")
        db.session.commit()
        assert is_available(ids["B"], ids["co"]) is True

    # admin can change another user's availability
    _login(client, ids["admin"])
    r = client.put(f"/api/phone/availability/user/{ids['B']}", json={"state": "away"})
    assert r.status_code == 200
    with app.app_context():
        assert is_available(ids["B"], ids["co"]) is False

    # ordinary user (viewer, no manage-users) cannot change another user
    _login(client, ids["plain"])
    r = client.put(f"/api/phone/availability/user/{ids['B']}", json={"state": "available"})
    assert r.status_code in (403, 404)
    with app.app_context():
        assert is_available(ids["B"], ids["co"]) is False  # unchanged

    # cross-tenant mutation prohibited (denied by tenant guard or license gate)
    _login(client, ids["outsider"])
    r = client.put(f"/api/phone/availability/user/{ids['B']}", json={"state": "available"})
    assert r.status_code in (402, 403, 404)
    with app.app_context():
        assert is_available(ids["B"], ids["co"]) is False


# =====================================================================
# C. PROMOTIONAL OPT-IN  (12-step + generic YES)
# =====================================================================

def test_C_promotional_optin_full_lifecycle(client, app):
    from services.promotional_optin import (
        classify_audience, create_solicitation, record_contextual_yes,
    )
    from services.contact_audience import resolve_sms_campaign_recipients
    from twilio_sms import _update_contact_sms_consent
    with app.app_context():
        co = _co("Promo Tenant")
        _twilio(co)
        c1 = _contact(co, "+14155551001"); _evidence(co, "+14155551001", "EV1")
        c2 = _contact(co, "+14155551002", sms_marketing_opt_in=True, sms_consent_status="opted_in"); _evidence(co, "+14155551002", "EV2")
        c3 = _contact(co, "+14155551003", sms_opted_out=True, sms_opt_out_at=datetime.utcnow()); _evidence(co, "+14155551003", "EV3")
        c4 = _contact(co, "+14155551004")
        c5 = _contact(co, "+14155551005"); _evidence(co, "+14155551005", "EV5")
        seg = _segment(co, [c1.id, c2.id, c3.id, c4.id, c5.id])
        db.session.commit()
        ids = dict(co=co.id, c1=c1.id, c2=c2.id, c3=c3.id, c4=c4.id, c5=c5.id, seg=seg.id)

    with app.app_context():
        counts = classify_audience(ids["co"], ids["seg"])["counts"]
        # overview categorizes all four (c5 is a second needs-optin contact)
        assert counts["my_order_customers"] == 5
        assert counts["already_promotional"] == 1          # c2
        assert counts["stop_suppressed"] == 1              # c3
        assert counts["no_conversational_evidence"] == 1   # c4
        assert counts["needs_promotional_optin"] == 2      # c1, c5

        # 1. create pending solicitation for c1
        r1 = create_solicitation(ids["co"], ids["c1"], business_phone_number="+15559999999", actor_user_id=None)
        assert r1["ok"] and r1["created"] is True
        # 2. idempotent
        r2 = create_solicitation(ids["co"], ids["c1"], business_phone_number="+15559999999", actor_user_id=None)
        assert r2["ok"] and r2["created"] is False
        assert PromotionalOptInSolicitation.query.filter_by(company_id=ids["co"], contact_id=ids["c1"]).count() == 1
        # 3. no SMS sent merely by creating / viewing
        assert TwilioMessage.query.filter_by(company_id=ids["co"], direction="outbound").count() == 0
        db.session.refresh(db.session.get(Contact, ids["c1"]))
        assert db.session.get(Contact, ids["c1"]).sms_marketing_opt_in is False
        classify_audience(ids["co"], ids["seg"])  # view audience -> still no consent change
        assert db.session.get(Contact, ids["c1"]).sms_marketing_opt_in is False
        db.session.commit()

    with app.app_context():
        seg_obj = db.session.get(Segment, ids["seg"])
        before = resolve_sms_campaign_recipients(_campaign(db.session.get(Company, ids["co"]), seg_obj, "promotional"))["counts"]
        assert before["eligible_recipients"] == 1  # c2 already promotional; c1 not yet

    with app.app_context():
        # 4. contextual YES
        out = record_contextual_yes(ids["co"], "+14155551001", "+15559999999", "YES-SID-1", keyword="yes")
        assert out["matched"] and out["granted"] is True
        db.session.commit()
        # 5. exactly one immutable consent event
        ev = PromotionalConsentEvent.query.filter_by(company_id=ids["co"], contact_id=ids["c1"]).all()
        assert len(ev) == 1
        assert ev[0].inbound_message_sid == "YES-SID-1"
        assert ev[0].canonical_phone == "+14155551001"
        assert ev[0].business_phone_number == "+15559999999"
        assert ev[0].consent_purpose == "promotional" and ev[0].consent_source == "sms_reply_yes"
        assert ev[0].solicited_at is not None
        # 6. solicitation -> consented
        sol = PromotionalOptInSolicitation.query.filter_by(company_id=ids["co"], contact_id=ids["c1"]).one()
        assert sol.status == "consented" and sol.consent_message_sid == "YES-SID-1"

    with app.app_context():
        seg_obj = db.session.get(Segment, ids["seg"])
        # 7. resolver now includes c1 exactly once (c1 + c2 = 2)
        after = resolve_sms_campaign_recipients(_campaign(db.session.get(Company, ids["co"]), seg_obj, "promotional"))["counts"]
        assert after["eligible_recipients"] == 2

    with app.app_context():
        # 8/9. replay same MessageSid -> no duplicate event
        dup = record_contextual_yes(ids["co"], "+14155551001", "+15559999999", "YES-SID-1", keyword="yes")
        assert dup["matched"] and dup.get("duplicate") is True
        db.session.commit()
        assert PromotionalConsentEvent.query.filter_by(company_id=ids["co"], contact_id=ids["c1"]).count() == 1
        seg_obj = db.session.get(Segment, ids["seg"])
        still = resolve_sms_campaign_recipients(_campaign(db.session.get(Company, ids["co"]), seg_obj, "promotional"))["counts"]
        assert still["eligible_recipients"] == 2

    with app.app_context():
        # 10/11. STOP -> resolver excludes c1
        _update_contact_sms_consent(ids["co"], "+14155551001", False, "keyword:stop")
        db.session.commit()
        seg_obj = db.session.get(Segment, ids["seg"])
        stopped = resolve_sms_campaign_recipients(_campaign(db.session.get(Company, ids["co"]), seg_obj, "promotional"))["counts"]
        assert stopped["eligible_recipients"] == 1  # only c2 remains

    with app.app_context():
        # 12. stale YES cannot restore promotional consent (solicitation already consumed/closed)
        stale = record_contextual_yes(ids["co"], "+14155551001", "+15559999999", "YES-SID-STALE", keyword="yes")
        assert stale.get("granted") is not True
        db.session.commit()
        assert PromotionalConsentEvent.query.filter_by(company_id=ids["co"], contact_id=ids["c1"]).count() == 1

    with app.app_context():
        # generic YES with NO pending solicitation -> nothing
        gen = record_contextual_yes(ids["co"], "+14155551005", "+15559999999", "YES-GEN", keyword="yes")
        assert gen["matched"] is False
        db.session.commit()
        c5 = db.session.get(Contact, ids["c5"])
        assert c5.sms_marketing_opt_in is False
        assert PromotionalConsentEvent.query.filter_by(company_id=ids["co"], contact_id=ids["c5"]).count() == 0


# =====================================================================
# D. CAMPAIGN RESOLVER — preview population == execution population
# =====================================================================

@pytest.mark.parametrize("purpose", ["promotional", "conversational", "transactional"])
def test_D_preview_equals_execution_population(app, purpose):
    from services.contact_audience import resolve_sms_campaign_recipients
    with app.app_context():
        co = _co(f"Resolver {purpose}")
        _twilio(co)
        promo = _contact(co, "+14155552001", sms_marketing_opt_in=True, sms_consent_status="opted_in")
        _evidence(co, "+14155552001", "RD1")
        conv_only = _contact(co, "+14155552002")
        _evidence(co, "+14155552002", "RD2")
        no_ev = _contact(co, "+14155552003")
        dup = _contact(co, "+14155552001b")  # different row, same normalized phone below
        dup.normalized_phone = "+14155552001"
        opted_out = _contact(co, "+14155552004", sms_opted_out=True, sms_opt_out_at=datetime.utcnow())
        _evidence(co, "+14155552004", "RD4")
        seg = _segment(co, [promo.id, conv_only.id, no_ev.id, dup.id, opted_out.id])
        db.session.commit()

        seg_obj = db.session.get(Segment, seg.id)
        camp = _campaign(db.session.get(Company, co.id), seg_obj, purpose)
        preview = resolve_sms_campaign_recipients(camp, materialize=False)
        execution = resolve_sms_campaign_recipients(camp, materialize=True)
        assert preview["counts"]["eligible_recipients"] == execution["counts"]["eligible_recipients"]
        pv = sorted(p for _, p in preview["recipients"])
        ex = sorted(p for _, p in execution["recipients"])
        assert pv == ex
        from models import SMSRecipient
        assert SMSRecipient.query.filter_by(company_id=co.id).count() == execution["counts"]["eligible_recipients"]


# =====================================================================
# E. ADMIN / SECURITY — newly affected resources
# =====================================================================

def test_E1_promotional_audience_denied_for_non_admin(client, app):
    with app.app_context():
        co = _co("E1 Tenant")
        _user(co, "e1_plain", role="viewer", manage=False, is_admin=False)
        plain_id = User.query.filter_by(username="e1_plain").one().id
        db.session.commit()
    _login(client, plain_id)
    with app.app_context():
        db.session.commit()
    assert client.get("/api/promotional-optin/audience").status_code == 403


def test_E2_promotional_endpoints_admin_tenant_isolation(client, app):
    with app.app_context():
        co_a = _co("E2 Tenant A")
        co_b = _co("E2 Tenant B")
        _user(co_a, "e2_admin_a", role="admin", manage=True, is_admin=True)
        admin_id = User.query.filter_by(username="e2_admin_a").one().id
        ca = _contact(co_a, "+14155554001"); _evidence(co_a, "+14155554001", "EE21")
        cb = _contact(co_b, "+14155554002"); _evidence(co_b, "+14155554002", "EE22")
        _segment(co_a, [ca.id]); _segment(co_b, [cb.id])
        db.session.commit()
        ids = dict(admin=admin_id, ca=ca.id, cb=cb.id)
    _login(client, ids["admin"])
    with app.app_context():
        db.session.commit()

    aud = client.get("/api/promotional-optin/audience")
    assert aud.status_code == 200
    rows = aud.get_json()["audience"]
    assert all(r["contact_id"] != ids["cb"] for r in rows)

    # cross-tenant mutation denied; nonexistent id safe
    assert client.post("/api/promotional-optin/solicitations", json={"contact_id": ids["cb"]}).status_code == 422
    assert client.post("/api/promotional-optin/solicitations", json={"contact_id": 987654}).status_code == 422
    assert client.get("/api/promotional-optin/overview?segment_id=987654").status_code == 404
