"""Minimal Tuya cloud client for notification relay commands."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
import requests


class TuyaError(RuntimeError):
    pass


@dataclass(frozen=True)
class TuyaConfig:
    base_url: str
    access_id: str
    access_secret: str
    device_id: str
    switch_code: str
    timeout_seconds: float


def sign_request(
    secret: str,
    client_id: str,
    timestamp: str,
    method: str,
    path_and_query: str,
    body: bytes = b"",
    access_token: str = "",
) -> str:
    """Create Tuya's uppercase HMAC-SHA256 request signature."""
    content_hash = hashlib.sha256(body).hexdigest()
    string_to_sign = f"{method.upper()}\n{content_hash}\n\n{path_and_query}"
    payload = f"{client_id}{access_token}{timestamp}{string_to_sign}"
    return (
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest().upper()
    )


class TuyaClient:
    def __init__(self, config: TuyaConfig, session=None, clock=time.time):
        self.config = config
        self.session = session or requests.Session()
        self.clock = clock
        self._token = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    def _request(self, method: str, path: str, *, body: bytes = b"", token: str = ""):
        timestamp = str(int(self.clock() * 1000))
        headers = {
            "client_id": self.config.access_id,
            "t": timestamp,
            "sign_method": "HMAC-SHA256",
            "sign": sign_request(
                self.config.access_secret,
                self.config.access_id,
                timestamp,
                method,
                path,
                body,
                token,
            ),
        }
        if token:
            headers["access_token"] = token
        if body:
            headers["Content-Type"] = "application/json"
        try:
            response = self.session.request(
                method,
                self.config.base_url.rstrip("/") + path,
                data=body or None,
                headers=headers,
                timeout=(self.config.timeout_seconds, self.config.timeout_seconds),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TuyaError(f"Tuya request failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            code = (
                payload.get("code", "malformed")
                if isinstance(payload, dict)
                else "malformed"
            )
            raise TuyaError(f"Tuya operation rejected (code={code})")
        return payload

    def get_token(self) -> str:
        now = self.clock()
        if self._token and now < self._token_expires_at:
            return self._token
        with self._token_lock:
            now = self.clock()
            if self._token and now < self._token_expires_at:
                return self._token
            payload = self._request("GET", "/v1.0/token?grant_type=1")
            result = payload.get("result")
            if not isinstance(result, dict) or not result.get("access_token"):
                raise TuyaError("Tuya token response malformed")
            try:
                expires = max(1, int(result.get("expire_time", 0)))
            except (TypeError, ValueError) as exc:
                raise TuyaError("Tuya token expiry malformed") from exc
            self._token = str(result["access_token"])
            self._token_expires_at = now + max(1, expires - min(60, expires * 0.1))
            return self._token

    def set_switch(self, value: bool) -> None:
        # The v2.0 cloud/thing/shadow/properties/issue endpoint is not a
        # supported control path for this device's category (cz / basic
        # switch-plug); it returns "device offline" for a device the
        # /v1.0/devices status endpoint reports as online, and its
        # response shape doesn't match this method's success check.
        # /v1.0/iot-03/devices/{id}/commands is the correct endpoint for
        # this device category and returns {"success": true, "result": true}.
        body = json.dumps(
            {"commands": [{"code": self.config.switch_code, "value": bool(value)}]},
            separators=(",", ":"),
        ).encode("utf-8")
        path = f"/v1.0/iot-03/devices/{self.config.device_id}/commands"
        payload = self._request("POST", path, body=body, token=self.get_token())
        if payload.get("result") is not True:
            raise TuyaError("Tuya command response malformed")
