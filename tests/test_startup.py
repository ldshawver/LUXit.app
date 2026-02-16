import os
import subprocess


def test_missing_session_secret_logs_warning():
    env = os.environ.copy()
    env.pop("SESSION_SECRET", None)
    env.pop("SECRET_KEY", None)
    env["CODEX_ENV"] = "production"
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env["OPENAI_API_KEY"] = "test"

    result = subprocess.run(
        ["python", "-c", "import app; print('ok')"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert "SESSION_SECRET is missing. Set it in your environment to start the app." in result.stderr


def test_app_imports_without_optional_env_vars():
    env = os.environ.copy()
    env["CODEX_ENV"] = "dev"
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env["SESSION_SECRET"] = "test-secret"
    for key in [
        "OPENAI_API_KEY",
        "REPL_ID",
        "TIKTOK_CLIENT_KEY",
        "TIKTOK_CLIENT_SECRET",
    ]:
        env.pop(key, None)

    result = subprocess.run(
        ["python", "-c", "import app; print('ok')"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0


def test_login_route_loads_without_optional_env_vars():
    env = os.environ.copy()
    env["CODEX_ENV"] = "dev"
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env["SESSION_SECRET"] = "test-secret"
    for key in [
        "OPENAI_API_KEY",
        "REPL_ID",
        "TIKTOK_CLIENT_KEY",
        "TIKTOK_CLIENT_SECRET",
    ]:
        env.pop(key, None)

    script = (
        "from app import app as flask_app;"
        "client = flask_app.test_client();"
        "response = client.get('/auth/login');"
        "print(response.status_code)"
    )
    result = subprocess.run(
        ["python", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "200"
