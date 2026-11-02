"""scripts/check_push_credentials.py: offline APNs/FCM credential validation."""
import json
import sys
import types

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from scripts import check_push_credentials as cpc


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _write(path, body, mode=0o600):
    path.write_text(body)
    path.chmod(mode)
    return str(path)


@pytest.fixture
def h2_present(monkeypatch):
    monkeypatch.setitem(sys.modules, "h2", types.ModuleType("h2"))


@pytest.fixture
def apns_env(tmp_path):
    return {
        "LUXIT_APNS_KEY_FILE": _write(tmp_path / "AuthKey.p8", _pem(ec.generate_private_key(ec.SECP256R1()))),
        "LUXIT_APNS_KEY_ID": "ABC123DEFG",
        "LUXIT_APNS_TEAM_ID": "TEAM123456",
        "LUXIT_APNS_TOPIC": "app.luxit.connect",
    }


@pytest.fixture
def fcm_env(tmp_path):
    info = {"type": "service_account", "project_id": "lux-connect", "client_email": "push@lux-connect.iam.gserviceaccount.com",
            "private_key": _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))}
    return {"LUXIT_FCM_SERVICE_ACCOUNT_FILE": _write(tmp_path / "sa.json", json.dumps(info))}


def test_valid_apns_and_fcm_pass(apns_env, fcm_env, h2_present):
    ok, report = cpc.run({**apns_env, **fcm_env}, {"apns", "fcm"})
    assert ok and report == {"apns": "ok", "fcm": "ok"}


def test_nothing_configured_fails():
    ok, report = cpc.run({}, set())
    assert not ok and report == {"apns": "not configured", "fcm": "not configured"}


def test_required_provider_missing_fails(fcm_env):
    ok, report = cpc.run(fcm_env, {"apns"})
    assert not ok and report["fcm"] == "ok"
    assert any("LUXIT_APNS_KEY_FILE is not set" in p for p in report["apns"])


def test_missing_h2_is_reported(apns_env, monkeypatch):
    monkeypatch.setitem(sys.modules, "h2", None)  # import h2 -> ImportError
    ok, report = cpc.run(apns_env, set())
    assert not ok and any("h2 package is missing" in p for p in report["apns"])


def test_apns_rejects_rsa_key_world_readable_file_and_bad_ids(tmp_path, h2_present):
    env = {
        "LUXIT_APNS_KEY_FILE": _write(tmp_path / "k.p8", _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048)), 0o644),
        "LUXIT_APNS_KEY_ID": "short", "LUXIT_APNS_TEAM_ID": "TEAM123456", "LUXIT_APNS_TOPIC": "app.luxit.connect.voip",
    }
    problems = cpc.check_apns(env)
    joined = " | ".join(problems)
    assert "readable by group/other" in joined
    assert "EC P-256" in joined
    assert "LUXIT_APNS_KEY_ID" in joined
    assert "not the .voip topic" in joined


def test_fcm_rejects_wrong_type_and_missing_fields(tmp_path):
    env = {"LUXIT_FCM_SERVICE_ACCOUNT_FILE": _write(tmp_path / "sa.json", json.dumps({"type": "authorized_user", "project_id": "p"}))}
    joined = " | ".join(cpc.check_fcm(env))
    assert "service_account" in joined and "client_email" in joined


def test_output_never_contains_key_material(apns_env, fcm_env, h2_present, monkeypatch, capsys):
    for k, v in {**apns_env, **fcm_env}.items():
        monkeypatch.setenv(k, v)
    assert cpc.main(["--require", "apns,fcm"]) == 0
    out = capsys.readouterr().out
    assert "PRIVATE KEY" not in out and '"pass": true' in out
