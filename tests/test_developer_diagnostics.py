import io, json, zipfile


def test_redaction_covers_secret_bank_and_pii(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGNOSTICS_LOG_DIR", str(tmp_path))
    from diagnostics_service import redact, structured_log, read_logs
    payload = {"password": "abc", "account_number": "123456789012", "note": "ssn 123-45-6789 token=abcd"}
    redacted = redact(payload)
    assert redacted["password"] == "[REDACTED_SECRET]"
    assert redacted["account_number"] == "[REDACTED_BANK]"
    assert "123-45-6789" not in redacted["note"]
    structured_log(level="error", service="payroll", message="failed token=abcd account 123456789012", metadata=payload)
    rows = read_logs("payroll")
    text = json.dumps(rows)
    assert "abcd" not in text
    assert "123456789012" not in text


def test_diagnostic_bundle_excludes_env_values(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGNOSTICS_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:super-secret@host/db")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_export")
    from diagnostics_service import export_bundle
    data = export_bundle()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        assert "json/environment.json" in names
        assert "logs/error.log" in names
        blob = b"".join(zf.read(n) for n in names)
    assert b"ghp_should_not_export" not in blob
    assert b"super-secret" not in blob
    env = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("json/environment.json"))
    assert env["DATABASE_URL_PRESENT"] is True
    assert env["GITHUB_TOKEN_PRESENT"] is True


def test_log_rotation_does_not_crash(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGNOSTICS_LOG_DIR", str(tmp_path))
    monkeypatch.setattr("diagnostics_service.MAX_LOG_SIZE", 1)
    from diagnostics_service import structured_log
    structured_log(level="info", service="app", message="first")
    structured_log(level="info", service="app", message="second")
    assert (tmp_path / "app.log").exists()
    assert (tmp_path / "app.log.1").exists()
