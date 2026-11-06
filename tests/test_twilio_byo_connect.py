"""Bring-your-own Twilio connect flow (T-32) with the Twilio REST client faked.

Covers: owner/admin only; credential format; Twilio rejection, trial and
suspended accounts; platform account and an account already connected
elsewhere refused; credentials stored encrypted; license granted only to a
paying workspace; numbers listed from the tenant's own account; attaching a
number proves ownership through their credentials, sets our webhooks and
refuses another workspace's number; disconnect clears our webhooks and wipes
credentials; every route needs the CSRF token.
"""
import types
import uuid

import pytest

from app import create_app
from extensions import db
from models import (Company, CompanyPlanPolicy, SecurityAuditEvent, TenantLicense, TwilioAccount,
                    TwilioPhoneNumber, User, UserCompanyAccess)
from services import twilio_connect
from services.license_service import PHONE_PWA_FEATURE


def _hex(n=32):
    return uuid.uuid4().hex[:n].ljust(n, "0")


class TwilioErr(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class FakeTwilio:
    """One fake Twilio cloud: accounts by SID, each with its token and numbers."""

    def __init__(self):
        self.accounts = {}
        self.calls = []

    def add_account(self, *, status="active", type_="Full"):
        sid, token = "AC" + _hex(), _hex()
        self.accounts[sid] = {"token": token, "status": status, "type": type_, "numbers": {}}
        return sid, token

    def add_number(self, sid, phone):
        nsid = "PN" + _hex()
        self.accounts[sid]["numbers"][nsid] = types.SimpleNamespace(
            sid=nsid, phone_number=phone, friendly_name=phone, sms_url="", voice_url="",
            capabilities={"sms": True, "voice": True, "mms": True})
        return nsid

    def client(self, sid, token):
        fake = self
        acct = self.accounts.get(sid)
        authed = acct is not None and acct["token"] == token

        def need_auth():
            if not authed:
                raise TwilioErr(401)

        class NumberCtx:
            def __init__(self, nsid):
                self.nsid = nsid

            def fetch(self):
                need_auth()
                n = acct["numbers"].get(self.nsid)
                if n is None:
                    raise TwilioErr(404)
                return n

            def update(self, **kw):
                need_auth()
                fake.calls.append(("update", self.nsid, kw))
                n = acct["numbers"][self.nsid]
                n.sms_url, n.voice_url = kw.get("sms_url", n.sms_url), kw.get("voice_url", n.voice_url)
                return n

        class Numbers:
            def __call__(self, nsid):
                return NumberCtx(nsid)

            def list(self, limit):
                need_auth()
                return list(acct["numbers"].values())

        def accounts(asid):
            def fetch():
                need_auth()
                a = fake.accounts[asid]
                return types.SimpleNamespace(sid=asid, status=a["status"], type=a["type"], friendly_name="Biz")
            return types.SimpleNamespace(fetch=fetch)

        return types.SimpleNamespace(api=types.SimpleNamespace(v2010=types.SimpleNamespace(accounts=accounts)),
                                     incoming_phone_numbers=Numbers())


@pytest.fixture
def world(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="localhost", WTF_CSRF_ENABLED=False)
    cloud = FakeTwilio()
    monkeypatch.setattr(twilio_connect, "_client", cloud.client)
    monkeypatch.setenv("LUXIT_TWILIO_WEBHOOK_BASE", "https://app.example.test")
    with app.app_context():
        tag = uuid.uuid4().hex[:8]
        a, b = Company(name=f"Byo A {tag}"), Company(name=f"Byo B {tag}")
        db.session.add_all([a, b])
        db.session.flush()
        users = {}
        for key, company, role in (("owner", a, "owner"), ("admin", a, "admin"), ("editor", a, "editor"),
                                   ("b_owner", b, "owner")):
            u = User(username=f"byo-{key}-{tag}", email=f"byo-{key}-{tag}@example.com", password_hash="x",
                     default_company_id=company.id)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            users[key] = u.id
        db.session.commit()
        yield app, cloud, {"a": a.id, "b": b.id}, users


def _req(app, uid, method, url, **kw):
    from flask import g
    g.pop("_login_user", None)
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
    return getattr(client, method)(url, **kw)


def _connect(app, uid, sid, token):
    return _req(app, uid, "post", "/api/comms/twilio/connect", json={"account_sid": sid, "auth_token": token})


def test_only_owner_or_admin(world):
    app, cloud, co, users = world
    sid, token = cloud.add_account()
    assert _connect(app, users["editor"], sid, token).status_code == 403
    assert _req(app, users["editor"], "get", "/api/comms/twilio").status_code == 403
    assert _connect(app, users["admin"], sid, token).status_code == 200


@pytest.mark.parametrize("case, code", [("format", "invalid_format"), ("wrong_token", "rejected"),
                                        ("trial", "trial_account"), ("suspended", "account_inactive")])
def test_bad_credentials_and_accounts_are_refused(world, case, code):
    app, cloud, co, users = world
    if case == "format":
        sid, token = "ACnot-a-sid", "x"
    elif case == "wrong_token":
        sid, _ = cloud.add_account()
        token = _hex()
    elif case == "trial":
        sid, token = cloud.add_account(type_="Trial")
    else:
        sid, token = cloud.add_account(status="suspended")
    r = _connect(app, users["owner"], sid, token)
    assert r.status_code == 400 and r.get_json()["code"] == code
    assert TwilioAccount.query.filter_by(company_id=co["a"]).count() == 0


def test_platform_account_and_double_connect_refused(world, monkeypatch):
    app, cloud, co, users = world
    sid, token = cloud.add_account()
    monkeypatch.setattr(twilio_connect, "_platform_account_sid", lambda: sid)
    r = _connect(app, users["owner"], sid, token)
    assert r.status_code == 403 and r.get_json()["code"] == "platform_account"
    monkeypatch.setattr(twilio_connect, "_platform_account_sid", lambda: None)
    assert _connect(app, users["b_owner"], sid, token).status_code == 200
    r = _connect(app, users["owner"], sid, token)
    assert r.status_code == 409 and r.get_json()["code"] == "in_use"


def test_connect_stores_encrypted_and_license_depends_on_plan(world):
    app, cloud, co, users = world
    sid, token = cloud.add_account()
    db.session.add(CompanyPlanPolicy(company_id=co["a"], plan_source="self_serve"))   # free self-serve workspace
    db.session.commit()
    r = _connect(app, users["owner"], sid, token)
    assert r.status_code == 200 and r.get_json()["license"] == "free_plan"
    ta = TwilioAccount.query.filter_by(company_id=co["a"]).one()
    assert ta.get_account_sid() == sid and ta.get_auth_token() == token
    assert token not in (ta._auth_token or "")                               # stored encrypted
    assert TenantLicense.query.filter_by(company_id=co["a"], feature_key=PHONE_PWA_FEATURE).count() == 0

    sid2, token2 = cloud.add_account()                                        # paying (managed) workspace
    r = _connect(app, users["b_owner"], sid2, token2)
    assert r.get_json()["license"] == "active"
    assert TenantLicense.query.filter_by(company_id=co["b"], feature_key=PHONE_PWA_FEATURE).one().status == "active"
    assert SecurityAuditEvent.query.filter_by(action="comms.twilio.connected", company_id=co["b"]).count() == 1


def test_numbers_come_from_their_account_and_attach_sets_webhooks(world):
    app, cloud, co, users = world
    sid, token = cloud.add_account()
    nsid = cloud.add_number(sid, "+14155550170")
    other_sid, _ = cloud.add_account()
    foreign = cloud.add_number(other_sid, "+14155550171")
    assert _connect(app, users["owner"], sid, token).status_code == 200

    listed = _req(app, users["owner"], "get", "/api/comms/twilio/numbers").get_json()["numbers"]
    assert [n["phone_number"] for n in listed] == ["+14155550170"]

    r = _req(app, users["owner"], "post", "/api/comms/twilio/numbers/attach", json={"number_sid": foreign})
    assert r.status_code == 404 and r.get_json()["code"] == "not_in_account"     # not in *their* account

    r = _req(app, users["owner"], "post", "/api/comms/twilio/numbers/attach", json={"number_sid": nsid})
    assert r.status_code == 200 and r.get_json()["primary"] is True
    pn = TwilioPhoneNumber.query.filter_by(phone_number="+14155550170").one()
    assert pn.company_id == co["a"] and pn.twilio_sid == nsid and pn.allow_global_fallback is False
    update = [c for c in cloud.calls if c[0] == "update"][-1][2]
    assert update["sms_url"] == "https://app.example.test/twilio/sms/inbound"
    assert update["voice_url"] == "https://app.example.test/twilio/voice/inbound"
    assert TwilioAccount.query.filter_by(company_id=co["a"]).one().from_phone == "+14155550170"


def test_cannot_attach_a_number_registered_to_another_workspace(world):
    app, cloud, co, users = world
    sid, token = cloud.add_account()
    nsid = cloud.add_number(sid, "+14155550172")
    db.session.add(TwilioPhoneNumber(company_id=co["b"], phone_number="+14155550172"))
    db.session.commit()
    assert _connect(app, users["owner"], sid, token).status_code == 200
    r = _req(app, users["owner"], "post", "/api/comms/twilio/numbers/attach", json={"number_sid": nsid})
    assert r.status_code == 409 and r.get_json()["code"] == "number_in_use"
    assert TwilioPhoneNumber.query.filter_by(phone_number="+14155550172").one().company_id == co["b"]


def test_disconnect_clears_our_webhooks_and_wipes_credentials(world):
    app, cloud, co, users = world
    sid, token = cloud.add_account()
    nsid = cloud.add_number(sid, "+14155550173")
    _connect(app, users["owner"], sid, token)
    _req(app, users["owner"], "post", "/api/comms/twilio/numbers/attach", json={"number_sid": nsid})
    r = _req(app, users["owner"], "post", "/api/comms/twilio/disconnect")
    assert r.status_code == 200 and r.get_json()["webhooks_cleared"] == 1
    assert cloud.accounts[sid]["numbers"][nsid].sms_url == ""
    ta = TwilioAccount.query.filter_by(company_id=co["a"]).one()
    assert ta.is_active is False and ta.get_account_sid() is None and ta.get_auth_token() is None
    assert TwilioPhoneNumber.query.filter_by(company_id=co["a"], is_active=True).count() == 0
    resp = _req(app, users["owner"], "get", "/api/comms/twilio")
    status = resp.get_json()
    assert resp.status_code == 200, (resp.status_code, status)
    assert status["connected"] is False


def test_connect_routes_need_csrf(world):
    app, cloud, co, users = world
    app.config["WTF_CSRF_ENABLED"] = True
    sid, token = cloud.add_account()
    for url in ("/api/comms/twilio/connect", "/api/comms/twilio/numbers/attach", "/api/comms/twilio/disconnect"):
        r = _req(app, users["owner"], "post", url, json={"account_sid": sid, "auth_token": token})
        assert r.status_code == 400 and r.get_json()["code"] == "csrf_failed", url
    assert TwilioAccount.query.filter_by(company_id=co["a"]).count() == 0
