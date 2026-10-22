import hashlib
import json
import threading
import time
import pytest
import requests

from services.tuya_client import TuyaClient, TuyaConfig, TuyaError, sign_request


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("bad status")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


class RaisingSession:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        raise self.error


def config(secret="test-secret"):
    return TuyaConfig(
        "https://openapi.tuyaus.com",
        "test-id",
        secret,
        "test-device-123",
        "switch_1",
        3,
    )


def test_signature_fixed_vector_and_exact_body_hash():
    body = b'{"properties":{"switch_1":true}}'
    signature = sign_request(
        "test-secret",
        "test-id",
        "1720000000123",
        "POST",
        "/v2.0/cloud/thing/test-device-123/shadow/properties/issue",
        body,
        "test-token",
    )
    assert (
        signature == "CFA80A34A25A5EB2974242D680EE58B9073ED9CD303C22D3998EB05180A8CDEB"
    )
    changed = sign_request(
        "test-secret",
        "test-id",
        "1720000000123",
        "POST",
        "/v2.0/cloud/thing/test-device-123/shadow/properties/issue",
        body + b" ",
        "test-token",
    )
    assert changed != signature
    assert hashlib.sha256(body).hexdigest() != hashlib.sha256(body + b" ").hexdigest()


def test_token_cache_refresh_and_exact_transmitted_json():
    now = [1000.0]
    session = Session(
        [
            Response(
                {
                    "success": True,
                    "result": {"access_token": "token-one", "expire_time": 100},
                }
            ),
            Response({"success": True, "result": True}),
            Response({"success": True, "result": True}),
            Response(
                {
                    "success": True,
                    "result": {"access_token": "token-two", "expire_time": 100},
                }
            ),
        ]
    )
    client = TuyaClient(config(), session=session, clock=lambda: now[0])
    assert client.get_token() == "token-one"
    client.set_switch(True)
    client.set_switch(False)
    assert len([c for c in session.calls if "/token?" in c[1]]) == 1
    expected_url = (
        "https://openapi.tuyaus.com/v1.0/iot-03/devices/test-device-123/commands"
    )
    for call, expected_value in zip(session.calls[1:3], (True, False)):
        method, url, request = call
        assert method == "POST"
        assert url == expected_url
        assert json.loads(request["data"]) == {
            "commands": [{"code": "switch_1", "value": expected_value}]
        }
    now[0] = 1091.0
    assert client.get_token() == "token-two"


def test_failures_are_sanitized_and_do_not_include_token_or_secret():
    session = Session([Response({"success": False, "code": 1010})])
    client = TuyaClient(config("never-print-this"), session=session, clock=lambda: 1000)
    with pytest.raises(TuyaError) as caught:
        client.get_token()
    text = str(caught.value)
    assert "never-print-this" not in text
    assert "access_token" not in text


@pytest.mark.parametrize(
    "response",
    [
        Response({}, status=503),
        Response(ValueError("invalid json")),
        Response(["malformed"]),
        Response({"success": False, "code": 1106, "msg": "denied"}),
    ],
)
def test_http_and_malformed_failures_are_sanitized(response):
    client = TuyaClient(
        config("top-secret"), session=Session([response]), clock=lambda: 1000
    )
    with pytest.raises(TuyaError) as caught:
        client.get_token()
    message = str(caught.value)
    assert "top-secret" not in message
    assert "sign" not in message.lower()


def test_timeout_is_bounded_and_sanitized():
    session = RaisingSession(requests.Timeout("contains-provider-detail"))
    client = TuyaClient(config(), session=session, clock=lambda: 1000)
    with pytest.raises(TuyaError) as caught:
        client.get_token()
    assert str(caught.value) == "Tuya request failed: Timeout"
    assert session.calls == 1


def test_concurrent_refresh_uses_one_token_request():
    class BlockingSession(Session):
        def request(self, method, url, **kwargs):
            time.sleep(0.02)
            return super().request(method, url, **kwargs)

    session = BlockingSession(
        [
            Response(
                {"success": True, "result": {"access_token": "one", "expire_time": 100}}
            ),
        ]
    )
    client = TuyaClient(config(), session=session, clock=lambda: 1000)
    values = []
    threads = [
        threading.Thread(target=lambda: values.append(client.get_token()))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert values == ["one"] * 8
    assert len(session.calls) == 1


def test_token_and_business_signatures_are_distinct_fixed_vectors():
    token_sign = sign_request(
        "test-secret", "test-id", "1720000000123", "GET", "/v1.0/token?grant_type=1"
    )
    business_sign = sign_request(
        "test-secret",
        "test-id",
        "1720000000123",
        "GET",
        "/v1.0/devices/test",
        access_token="token",
    )
    assert (
        token_sign == "A714CE8DCB6E053B78A93079DAEC154DFB053B2DB49E85C3243AFE05CDEA4D14"
    )
    assert (
        business_sign
        == "49489702CD37DA2610E48A657EEE70A3062E40D94723F41F8C13FEDE5BE3D97B"
    )


def test_source_contract_has_no_browser_or_in_process_timer():
    source = open("services/tuya_notification.py", encoding="utf-8").read()
    assert "time.sleep" not in source
    assert "threading.Timer" not in source
    assert "setTimeout" not in source
