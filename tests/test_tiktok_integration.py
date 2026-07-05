import io
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app import app, db
from models import Company, TikTokOAuth, TikTokPost, User
from services.tiktok_service import TikTokService


@pytest.fixture
def client(monkeypatch):
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs")
    monkeypatch.setenv("TIKTOK_REDIRECT_URI", "http://127.0.0.1:8765/callback/")
    monkeypatch.setenv("TIKTOK_SCOPES", "user.info.basic video.publish")
    monkeypatch.setenv("TIKTOK_ALLOWED_MEDIA_DOMAINS", "cdn.example.com")
    with app.app_context():
        db.create_all()
        company = Company(name="TikTok Test Co")
        user = User(username="tiktok-user", email="tiktok@example.com", default_company=company)
        db.session.add_all([company, user]); db.session.commit()
        user.default_company_id = company.id; user.ensure_company_access(company.id, "owner"); db.session.commit()
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["_user_id"] = str(user.id)
                sess["_fresh"] = True
            c.user_id = user.id; c.company_id = company.id
            yield c
        db.session.remove(); db.drop_all()


def account(user_id, company_id, **kw):
    a = TikTokOAuth(user_id=user_id, company_id=company_id, open_id=kw.get("open_id", "open"), scope=kw.get("scope", "user.info.basic video.publish"), expires_at=kw.get("expires_at", datetime.utcnow()+timedelta(hours=1)), status="active", creator_info=kw.get("creator_info", {"privacy_level_options": ["SELF_ONLY"]}))
    a.set_access_token("access-secret"); a.set_refresh_token("refresh-secret")
    db.session.add(a); db.session.commit(); return a


def test_pkce_verifier_challenge_generation():
    verifier = TikTokService.generate_code_verifier()
    challenge = TikTokService.code_challenge(verifier)
    assert len(verifier) >= 43
    assert len(challenge) == 64
    assert challenge == TikTokService.code_challenge(verifier)


def test_oauth_url_generation(client):
    r = client.get("/api/oauth/tiktok/start")
    assert r.status_code == 302
    q = parse_qs(urlparse(r.location).query)
    assert r.location.startswith(TikTokService.AUTH_URL)
    assert q["client_key"] == ["ck"]
    assert q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8765/callback/"]
    assert q["code_challenge_method"] == ["S256"]
    assert len(q["code_challenge"][0]) == 64


def test_state_mismatch_rejection(client):
    with client.session_transaction() as s:
        s["tiktok_oauth_state"] = "good"
    r = client.get("/api/oauth/tiktok/callback?code=x&state=bad")
    assert r.status_code == 400
    assert r.json["error"] == "state_mismatch"


def test_tiktok_callback_error_handling(client):
    r = client.get("/api/oauth/tiktok/callback?error=access_denied&error_description=nope")
    assert r.status_code == 400
    assert r.json["error"] == "access_denied"


def test_successful_token_exchange(client, monkeypatch):
    with client.session_transaction() as s:
        s["tiktok_oauth_state"] = "state"; s["tiktok_pkce_verifier"] = "verifier"; s["tiktok_oauth_company_id"] = client.company_id
    monkeypatch.setattr(TikTokService, "exchange_code_for_token", lambda self, code, code_verifier=None, redirect_uri=None: {"success": True, "access_token": "at", "refresh_token": "rt", "open_id": "oid", "scope": "user.info.basic video.publish", "expires_at": datetime.utcnow()+timedelta(hours=1)})
    monkeypatch.setattr(TikTokService, "get_user_info", lambda self, at, oid=None: {"success": True, "display_name": "Creator", "avatar_url": "https://cdn.example.com/a.png"})
    r = client.get("/api/oauth/tiktok/callback?code=abc&state=state")
    assert r.status_code == 302
    saved = TikTokOAuth.query.filter_by(open_id="oid").first()
    assert saved and saved.get_access_token() == "at"


def test_creator_info_query_and_token_secrecy(client, monkeypatch):
    account(client.user_id, client.company_id)
    monkeypatch.setattr(TikTokService, "query_creator_info", lambda self, token: {"success": True, "data": {"creator_username": "lux", "privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 600}})
    r = client.get("/api/integrations/tiktok/creator-info")
    assert r.status_code == 200 and r.json["success"]
    assert "access-secret" not in r.get_data(as_text=True)
    assert "refresh-secret" not in r.get_data(as_text=True)


def test_missing_tiktok_token(client):
    r = client.get("/api/integrations/tiktok/creator-info")
    assert r.status_code == 404


def test_expired_token_refresh(client, monkeypatch):
    account(client.user_id, client.company_id, expires_at=datetime.utcnow()-timedelta(minutes=1))
    monkeypatch.setattr(TikTokService, "refresh_access_token", lambda self, rt: {"success": True, "access_token": "new", "refresh_token": "newr", "expires_at": datetime.utcnow()+timedelta(hours=2), "scope": "user.info.basic video.publish"})
    monkeypatch.setattr(TikTokService, "query_creator_info", lambda self, token: {"success": True, "data": {"creator_username": "lux", "privacy_level_options": ["SELF_ONLY"]}})
    r = client.get("/api/integrations/tiktok/creator-info")
    assert r.status_code == 200
    assert TikTokOAuth.query.first().get_access_token() == "new"


def test_video_file_upload_init_and_content_range(client, monkeypatch):
    account(client.user_id, client.company_id)
    calls = {}
    monkeypatch.setattr(TikTokService, "init_video", lambda self, token, post_info, source_info: calls.setdefault("init", {"success": True, "data": {"publish_id": "p1", "upload_url": "https://upload"}}))
    def upload(self, url, chunk, start, end, total):
        calls["range"] = f"bytes {start}-{end}/{total}"; return {"success": True}
    monkeypatch.setattr(TikTokService, "upload_video_chunk", upload)
    r = client.post("/api/integrations/tiktok/posts/video", data={"source": "FILE_UPLOAD", "privacy_level": "SELF_ONLY", "video": (io.BytesIO(b"12345"), "v.mp4")}, content_type="multipart/form-data")
    assert r.status_code == 200 and r.json["publish_id"] == "p1"
    assert calls["range"] == "bytes 0-4/5"


def test_video_pull_from_url_init(client, monkeypatch):
    account(client.user_id, client.company_id)
    monkeypatch.setattr(TikTokService, "init_video", lambda self, token, post_info, source_info: {"success": True, "data": {"publish_id": "p2"}})
    r = client.post("/api/integrations/tiktok/posts/video", json={"source": "PULL_FROM_URL", "video_url": "https://cdn.example.com/v.mp4", "privacy_level": "SELF_ONLY"})
    assert r.status_code == 200 and r.json["publish_id"] == "p2"


def test_photo_content_init(client, monkeypatch):
    account(client.user_id, client.company_id)
    monkeypatch.setattr(TikTokService, "init_photo", lambda self, token, post_info, source_info: {"success": True, "data": {"publish_id": "ph1"}})
    r = client.post("/api/integrations/tiktok/posts/photo", json={"photo_images": ["https://cdn.example.com/a.jpg"], "privacy_level": "SELF_ONLY"})
    assert r.status_code == 200 and r.json["publish_id"] == "ph1"


def test_status_fetch(client, monkeypatch):
    a = account(client.user_id, client.company_id)
    db.session.add(TikTokPost(company_id=client.company_id, user_id=client.user_id, tiktok_account_id=a.id, publish_id="p1", media_type="video", source_type="pull_from_url")); db.session.commit()
    monkeypatch.setattr(TikTokService, "fetch_status", lambda self, token, publish_id: {"success": True, "data": {"status": "PUBLISH_COMPLETE"}})
    r = client.get("/api/integrations/tiktok/posts/p1/status")
    assert r.status_code == 200 and r.json["data"]["status"] == "PUBLISH_COMPLETE"


def test_invalid_privacy_missing_scope_unverified_url_rejections(client):
    account(client.user_id, client.company_id, scope="user.info.basic")
    r = client.post("/api/integrations/tiktok/posts/video", json={"source": "PULL_FROM_URL", "video_url": "https://cdn.example.com/v.mp4", "privacy_level": "SELF_ONLY"})
    assert r.status_code == 400 and "video.publish" in r.json["error"]
    TikTokOAuth.query.delete(); db.session.commit()
    account(client.user_id, client.company_id)
    assert client.post("/api/integrations/tiktok/posts/video", json={"source": "PULL_FROM_URL", "video_url": "https://evil.example/v.mp4", "privacy_level": "SELF_ONLY"}).status_code == 400
    assert client.post("/api/integrations/tiktok/posts/video", json={"source": "PULL_FROM_URL", "video_url": "https://cdn.example.com/v.mp4", "privacy_level": "PUBLIC_TO_EVERYONE"}).status_code == 400


def test_disconnect_flow_and_tenant_isolation(client, monkeypatch):
    a = account(client.user_id, client.company_id)
    other_co = Company(name="Other"); other_user = User(username="other", email="other@example.com", default_company=other_co)
    db.session.add_all([other_co, other_user]); db.session.commit()
    other = account(other_user.id, other_co.id, open_id="other")
    monkeypatch.setattr(TikTokService, "revoke_token", lambda *args, **kwargs: {"success": True})
    r = client.post("/api/integrations/tiktok/disconnect", json={"account_id": a.id})
    assert r.status_code == 200 and r.json["success"]
    assert db.session.get(TikTokOAuth, a.id).status == "disconnected"
    assert db.session.get(TikTokOAuth, other.id).status == "active"


def test_wildcard_registration_resolves_to_concrete_runtime_redirect(monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs")
    monkeypatch.setenv("TIKTOK_REDIRECT_URI", "http://127.0.0.1:*/callback/")
    monkeypatch.setenv("TIKTOK_DESKTOP_CALLBACK_PORT", "3455")
    svc = TikTokService()
    ok, error = svc.validate_configuration()
    auth_url, _, _ = svc.build_auth_url(state="s", code_verifier="v")
    redirect = parse_qs(urlparse(auth_url).query)["redirect_uri"][0]
    assert ok, error
    assert redirect == "http://127.0.0.1:3455/callback/"
    assert "*" not in redirect


def test_web_mode_requires_https_concrete_non_loopback(monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs")
    monkeypatch.setenv("TIKTOK_OAUTH_MODE", "web")
    monkeypatch.setenv("TIKTOK_REDIRECT_URI", "https://app.example.com/api/oauth/tiktok/callback")
    svc = TikTokService()
    assert svc.validate_configuration()[0]
    assert parse_qs(urlparse(svc.build_auth_url(state="s", code_verifier="v")[0]).query)["redirect_uri"][0].startswith("https://app.example.com/")


def test_invalid_enabled_redirect_configuration_rejected(monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "ck")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "cs")
    monkeypatch.setenv("TIKTOK_REDIRECT_URI", "http://example.com:*/callback/")
    svc = TikTokService()
    ok, error = svc.validate_configuration()
    assert not ok
    assert "TIKTOK_REDIRECT_URI" in error


def test_connections_saves_tiktok_config_and_oauth_reads_db(client, monkeypatch):
    monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
    monkeypatch.delenv("TIKTOK_CLIENT_SECRET", raising=False)
    payload = {
        "TIKTOK_CLIENT_KEY": "db-client-key",
        "TIKTOK_CLIENT_SECRET": "db-client-secret",
        "TIKTOK_REDIRECT_URI": "http://127.0.0.1:8765/callback/",
        "TIKTOK_OAUTH_MODE": "desktop",
        "TIKTOK_SCOPES": "user.info.basic video.publish",
        "TIKTOK_ALLOWED_MEDIA_DOMAINS": "db-cdn.example.com",
    }
    r = client.post(f"/api/company/{client.company_id}/secrets/save", json=payload)
    assert r.status_code == 200 and r.json["success"]

    service = TikTokService.from_company(db.session.get(Company, client.company_id))
    assert service.client_key == "db-client-key"
    assert service.client_secret == "db-client-secret"
    assert service.scopes == ["user.info.basic", "video.publish"]

    r = client.get("/api/oauth/tiktok/start")
    assert r.status_code == 302
    q = parse_qs(urlparse(r.location).query)
    assert q["client_key"] == ["db-client-key"]


def test_test_configuration_and_write_only_secret_response(client):
    payload = {
        "TIKTOK_CLIENT_KEY": "write-only-key",
        "TIKTOK_CLIENT_SECRET": "super-secret-value",
        "TIKTOK_REDIRECT_URI": "http://127.0.0.1:8765/callback/",
        "TIKTOK_OAUTH_MODE": "desktop",
        "TIKTOK_SCOPES": "user.info.basic video.publish",
    }
    assert client.post(f"/api/company/{client.company_id}/secrets/save", json=payload).json["success"]
    test = client.post(f"/api/company/{client.company_id}/integrations/tiktok/test", json={})
    assert test.status_code == 200 and test.json["success"]
    assert test.json["checks"]["runtime_can_resolve_config"]

    listed = client.get(f"/api/company/{client.company_id}/secrets")
    body = listed.get_data(as_text=True)
    assert listed.status_code == 200
    assert "super-secret-value" not in body
    assert "write-only-key" not in body


def test_env_fallback_and_specific_missing_diagnostics(client, monkeypatch):
    company = db.session.get(Company, client.company_id)
    assert TikTokService.from_company(company).client_key == "ck"
    monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
    monkeypatch.delenv("TIKTOK_CLIENT_SECRET", raising=False)
    svc = TikTokService.from_company(company)
    ok, error = svc.validate_configuration()
    assert not ok
    assert "client key and client secret" in error
    assert "No key found in DB or env" not in error


def test_legacy_integration_pages_redirect_to_connections(client):
    for path in ("/platform/api-hub", "/global-admin/integrations", "/platform/integrations"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302
        assert "/connections" in r.location


def test_company_scoped_tiktok_config_does_not_cross_contaminate(client, monkeypatch):
    monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
    monkeypatch.delenv("TIKTOK_CLIENT_SECRET", raising=False)
    company = db.session.get(Company, client.company_id)
    other = Company(name="Other TikTok Config")
    db.session.add(other); db.session.commit()
    company.set_secret("TIKTOK_CLIENT_KEY", "tenant-one")
    company.set_secret("TIKTOK_CLIENT_SECRET", "tenant-one-secret")
    other.set_secret("TIKTOK_CLIENT_KEY", "tenant-two")
    other.set_secret("TIKTOK_CLIENT_SECRET", "tenant-two-secret")
    assert TikTokService.from_company(company).client_key == "tenant-one"
    assert TikTokService.from_company(other).client_key == "tenant-two"
