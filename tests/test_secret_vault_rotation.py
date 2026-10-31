"""SecretVault key rotation and production fail-closed (P0-9 remediation)."""
import base64
import importlib
import json
import os
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import rotate_encryption_key as rot  # noqa: E402

from services import secret_vault  # noqa: E402

OLD, NEW, OTHER = (Fernet.generate_key().decode() for _ in range(3))


def _vault(monkeypatch, primary=None, previous=None, env=None):
    for k, v in (('ENCRYPTION_MASTER_KEY', primary), ('ENCRYPTION_MASTER_KEY_PREVIOUS', previous), ('FLASK_ENV', env)):
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return secret_vault.SecretVault()


def test_new_key_reads_old_values_and_writes_with_new(monkeypatch):
    stored = _vault(monkeypatch, OLD).encrypt('sk_test_123')
    vault = _vault(monkeypatch, NEW, OLD)
    assert vault.decrypt(stored) == 'sk_test_123'
    fresh = vault.encrypt('x')
    assert _vault(monkeypatch, NEW).decrypt(fresh) == 'x'           # no old key needed
    rotated = vault.rotate(stored)
    assert _vault(monkeypatch, NEW).decrypt(rotated) == 'sk_test_123'
    with pytest.raises(Exception):
        _vault(monkeypatch, OLD).decrypt(rotated)


@pytest.mark.parametrize('primary,previous', [(None, None), ('not-a-key', None), (NEW, 'bad')])
def test_production_refuses_missing_or_malformed_keys(monkeypatch, primary, previous):
    with pytest.raises(RuntimeError):
        _vault(monkeypatch, primary, previous, env='production')


def test_development_without_key_uses_ephemeral_key_and_never_logs_it(monkeypatch, caplog):
    vault = _vault(monkeypatch, None, None, env='development')
    assert vault.decrypt(vault.encrypt('a')) == 'a'
    assert 'Add to secrets' not in caplog.text


def _enc(key, text):
    return base64.b64encode(Fernet(key.encode()).encrypt(text.encode())).decode()


def test_rotator_handles_plain_json_nested_and_foreign_values():
    r = rot.Rotator(secret_vault.load_keys(NEW, OLD))
    new, changed = r.cell(_enc(OLD, 'tok'), 'text')
    assert changed and Fernet(NEW.encode()).decrypt(base64.b64decode(new)) == b'tok'

    current = _enc(NEW, 'tok')
    assert r.cell(current, 'text') == (current, False)

    doc = json.dumps({'page1': _enc(OLD, 'p1'), 'meta': {'list': [_enc(OLD, 'p2'), 'plain']}, 'n': 3})
    new, changed = r.cell(doc, 'text')
    out = json.loads(new)
    assert changed and out['n'] == 3 and out['meta']['list'][1] == 'plain'
    assert Fernet(NEW.encode()).decrypt(base64.b64decode(out['meta']['list'][0])) == b'p2'

    jsonb, changed = r.cell({'k': _enc(OLD, 'v')}, 'jsonb')
    assert changed and isinstance(jsonb, str)

    foreign = _enc(OTHER, 'x')
    assert r.cell(foreign, 'text') == (foreign, False)
    assert r.stats['undecryptable'] == 1 and r.stats['old_key'] == 4


def test_rotator_requires_old_and_new_keys():
    with pytest.raises(SystemExit):
        rot.Rotator(secret_vault.load_keys(NEW, ''))
