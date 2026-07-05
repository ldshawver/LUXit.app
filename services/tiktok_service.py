"""TikTok OAuth and Content Posting API service helpers."""
import hashlib
import math
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, urlunparse

import requests


class TikTokService:
    AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
    TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
    REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
    USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
    CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
    VIDEO_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    CONTENT_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/content/init/"
    STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

    def __init__(
        self,
        client_key=None,
        client_secret=None,
        redirect_uri=None,
        scopes=None,
        runtime_redirect_uri=None,
        oauth_mode=None,
        allowed_media_domains=None,
    ):
        self.client_key = client_key or os.getenv("TIKTOK_CLIENT_KEY")
        self.client_secret = client_secret or os.getenv("TIKTOK_CLIENT_SECRET")
        # Registration URI may be the TikTok console wildcard. It is never sent
        # in OAuth requests unless it is already a concrete valid runtime URI.
        self.redirect_uri = redirect_uri or os.getenv(
            "TIKTOK_REDIRECT_URI", "http://127.0.0.1:*/callback/"
        )
        self.runtime_redirect_uri = runtime_redirect_uri or os.getenv(
            "TIKTOK_RUNTIME_REDIRECT_URI"
        )
        self.oauth_mode = (oauth_mode or os.getenv("TIKTOK_OAUTH_MODE", "desktop")).lower()
        raw_scopes = scopes if scopes is not None else os.getenv("TIKTOK_SCOPES", "user.info.basic")
        if isinstance(raw_scopes, str):
            self.scopes = raw_scopes.replace(",", " ").split()
        else:
            self.scopes = list(raw_scopes or [])
        self.allowed_media_domains = self._parse_domains(
            allowed_media_domains or os.getenv("TIKTOK_ALLOWED_MEDIA_DOMAINS", "localhost,127.0.0.1")
        )

    @staticmethod
    def _parse_domains(value):
        return [d.strip().lower() for d in str(value or "").replace("\n", ",").split(",") if d.strip()]

    @classmethod
    def from_company(cls, company):
        def secret(name):
            return company.get_secret(name) if company and hasattr(company, "get_secret") else None
        return cls(
            secret("TIKTOK_CLIENT_KEY"),
            secret("TIKTOK_CLIENT_SECRET"),
            secret("TIKTOK_REDIRECT_URI"),
            secret("TIKTOK_SCOPES"),
            secret("TIKTOK_RUNTIME_REDIRECT_URI"),
            secret("TIKTOK_OAUTH_MODE"),
            secret("TIKTOK_ALLOWED_MEDIA_DOMAINS"),
        )

    @staticmethod
    def _has_wildcard_port(uri):
        return urlparse(uri).netloc.endswith(":*")

    @staticmethod
    def _is_loopback_host(hostname):
        return hostname in {"localhost", "127.0.0.1", "::1"}

    @classmethod
    def is_valid_redirect_uri(cls, uri, mode="desktop", allow_wildcard=False):
        parsed = urlparse(uri or "")
        mode = (mode or "desktop").lower()
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if cls._has_wildcard_port(uri):
            return bool(
                allow_wildcard
                and mode == "desktop"
                and parsed.scheme == "http"
                and cls._is_loopback_host(parsed.hostname)
            )
        if mode == "desktop":
            return (
                parsed.port is not None
                and parsed.scheme == "http"
                and cls._is_loopback_host(parsed.hostname)
            )
        if mode == "web":
            return parsed.scheme == "https" and not cls._is_loopback_host(parsed.hostname)
        return False

    def resolved_redirect_uri(self):
        candidate = self.runtime_redirect_uri or self.redirect_uri
        if self._has_wildcard_port(candidate):
            port = os.getenv("TIKTOK_DESKTOP_CALLBACK_PORT", "3455")
            parsed = urlparse(candidate)
            candidate = urlunparse(
                parsed._replace(netloc=f"{parsed.hostname}:{port}")
            )
        return candidate

    def validate_configuration(self):
        if not (self.client_key or self.client_secret):
            return False, "TikTok OAuth client key and client secret are missing in company settings and environment."
        if not self.client_key:
            return False, "TikTok client key is missing in company settings and TIKTOK_CLIENT_KEY."
        if not self.client_secret:
            return False, "TikTok client secret is missing in company settings and TIKTOK_CLIENT_SECRET."
        if not self.scopes:
            return False, "TikTok scopes are missing in company settings and TIKTOK_SCOPES."
        if not self.is_valid_redirect_uri(
            self.redirect_uri,
            self.oauth_mode,
            allow_wildcard=self.oauth_mode == "desktop",
        ):
            return False, "TIKTOK_REDIRECT_URI is not valid for the configured TikTok OAuth mode."
        runtime_uri = self.resolved_redirect_uri()
        if "*" in urlparse(runtime_uri).netloc or not self.is_valid_redirect_uri(
            runtime_uri,
            self.oauth_mode,
            allow_wildcard=False,
        ):
            return False, "TikTok runtime redirect URI must be concrete and valid."
        return True, None

    def configuration_diagnostics(self):
        ok, error = self.validate_configuration()
        return {
            "ok": ok,
            "error": error,
            "client_key_configured": bool(self.client_key),
            "client_secret_configured": bool(self.client_secret),
            "redirect_uri_configured": bool(self.redirect_uri),
            "redirect_uri_valid": self.is_valid_redirect_uri(self.redirect_uri, self.oauth_mode, allow_wildcard=self.oauth_mode == "desktop"),
            "runtime_redirect_uri": self.resolved_redirect_uri(),
            "runtime_redirect_uri_valid": self.is_valid_redirect_uri(self.resolved_redirect_uri(), self.oauth_mode, allow_wildcard=False),
            "scopes_configured": bool(self.scopes),
            "oauth_mode": self.oauth_mode,
            "allowed_media_domains_configured": bool(self.allowed_media_domains),
        }

    def is_configured(self):
        ok, _ = self.validate_configuration()
        return bool(ok and self.client_key and self.client_secret)

    @staticmethod
    def generate_state():
        return secrets.token_urlsafe(32)

    @staticmethod
    def generate_code_verifier():
        return secrets.token_urlsafe(64)[:128]

    @staticmethod
    def code_challenge(verifier):
        return hashlib.sha256(verifier.encode("utf-8")).hexdigest()

    def build_auth_url(self, state=None, code_verifier=None, redirect_uri=None):
        state = state or self.generate_state()
        code_verifier = code_verifier or self.generate_code_verifier()
        params = {
            "client_key": self.client_key,
            "response_type": "code",
            "scope": ",".join(self.scopes),
            "redirect_uri": redirect_uri or self.resolved_redirect_uri(),
            "state": state,
            "code_challenge": self.code_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{self.AUTH_URL}?{urlencode(params)}", state, code_verifier

    def _token_result(self, token_data):
        if token_data.get("error"):
            return {"success": False, "error": token_data.get("error_description") or token_data.get("error")}
        now = datetime.utcnow()
        return {
            "success": True,
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "open_id": token_data.get("open_id"),
            "scope": token_data.get("scope") or token_data.get("scopes"),
            "token_type": token_data.get("token_type", "Bearer"),
            "expires_at": now + timedelta(seconds=int(token_data.get("expires_in", 86400))),
            "refresh_expires_at": now + timedelta(seconds=int(token_data.get("refresh_expires_in", 31536000))),
        }

    def exchange_code_for_token(self, code, code_verifier=None, redirect_uri=None):
        data = {"client_key": self.client_key, "client_secret": self.client_secret, "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri or self.resolved_redirect_uri()}
        if code_verifier:
            data["code_verifier"] = code_verifier
        return self._token_result(requests.post(self.TOKEN_URL, data=data, timeout=15).json())

    def refresh_access_token(self, refresh_token):
        data = {"client_key": self.client_key, "client_secret": self.client_secret, "grant_type": "refresh_token", "refresh_token": refresh_token}
        return self._token_result(requests.post(self.TOKEN_URL, data=data, timeout=15).json())

    def revoke_token(self, token, open_id=None):
        requests.post(self.REVOKE_URL, data={"client_key": self.client_key, "client_secret": self.client_secret, "token": token}, timeout=15)
        return {"success": True}

    def _post(self, url, access_token, payload=None):
        r = requests.post(url, headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}, json=payload or {}, timeout=30)
        data = r.json()
        err = data.get("error") or {}
        if err and err.get("code") not in (None, "ok"):
            return {"success": False, "error": err.get("message") or err.get("code"), "error_code": err.get("code")}
        return {"success": True, "data": data.get("data", data)}

    def get_user_info(self, access_token, open_id=None):
        r = requests.get(self.USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"}, params={"fields": "open_id,avatar_url,display_name"}, timeout=15)
        user = r.json().get("data", {}).get("user", {})
        return {"success": True, "open_id": user.get("open_id"), "display_name": user.get("display_name"), "avatar_url": user.get("avatar_url")}

    def query_creator_info(self, access_token):
        return self._post(self.CREATOR_INFO_URL, access_token)

    def init_video(self, access_token, post_info, source_info):
        return self._post(self.VIDEO_INIT_URL, access_token, {"post_info": post_info, "source_info": source_info})

    def upload_video_chunk(self, upload_url, chunk, start, end, total):
        r = requests.put(upload_url, headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end}/{total}"}, data=chunk, timeout=60)
        r.raise_for_status()
        return {"success": True, "content_range": f"bytes {start}-{end}/{total}"}

    def init_photo(self, access_token, post_info, source_info):
        return self._post(self.CONTENT_INIT_URL, access_token, {"post_mode": "DIRECT_POST", "media_type": "PHOTO", "post_info": post_info, "source_info": source_info})

    def fetch_status(self, access_token, publish_id):
        return self._post(self.STATUS_URL, access_token, {"publish_id": publish_id})

    def list_videos(self, access_token, open_id=None, cursor=None, max_count=20):
        r = requests.post(
            "https://open.tiktokapis.com/v2/video/list/",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            params={"fields": "id,title,video_description,duration,cover_image_url,create_time,share_url"},
            json={"cursor": cursor, "max_count": min(int(max_count), 20)} if cursor else {"max_count": min(int(max_count), 20)},
            timeout=30,
        )
        data = r.json()
        err = data.get("error") or {}
        if err and err.get("code") not in (None, "ok"):
            return {"success": False, "error": err.get("message") or err.get("code")}
        return {"success": True, **data.get("data", {})}

    def publish_video(self, access_token, title, description="", video_url=None, privacy_level="SELF_ONLY", disable_duet=False, disable_comment=False, disable_stitch=False):
        return self.init_video(access_token, {"title": (title or "")[:150], "privacy_level": privacy_level, "disable_duet": disable_duet, "disable_comment": disable_comment, "disable_stitch": disable_stitch}, {"source": "PULL_FROM_URL", "video_url": video_url})

    def check_publish_status(self, access_token, publish_id):
        return self.fetch_status(access_token, publish_id)


def is_allowed_media_url(url, company=None):
    service = TikTokService.from_company(company) if company else TikTokService()
    allowed = service.allowed_media_domains
    host = (urlparse(url).hostname or "").lower()
    return bool(host and any(host == d or host.endswith("." + d) for d in allowed))


def chunk_plan(size, chunk_size=10 * 1024 * 1024):
    return chunk_size, int(math.ceil(size / float(chunk_size)))
