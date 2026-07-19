import os
import subprocess
from pathlib import Path


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
    assert result.stdout.strip().endswith("200")


def test_canonical_app_import_user_loader_registered_and_rolls_back_on_db_error():
    env = os.environ.copy()
    env["CODEX_ENV"] = "dev"
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env["SESSION_SECRET"] = "test-secret"
    script = """
from app import app
import app as app_module
from unittest.mock import patch
assert app.login_manager._user_callback is not None
called = {'rollback': False}
with patch.object(app_module.db.session, 'get', side_effect=Exception('db boom')):
    with patch.object(app_module.db.session, 'rollback', side_effect=lambda: called.__setitem__('rollback', True)):
        assert app.login_manager._user_callback('1') is None
assert called['rollback'] is True
print('ok')
"""
    result = subprocess.run(["python", "-c", script], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr


def test_user_loader_rejects_archived_and_loads_active_user():
    env = os.environ.copy()
    env["CODEX_ENV"] = "dev"
    env["DATABASE_URL"] = "sqlite:///:memory:"
    env["SESSION_SECRET"] = "test-secret"
    script = """
from app import app
import app as app_module
from unittest.mock import patch
class U:
    def __init__(self, active): self.is_active = active
users = {1: U(True), 2: U(False)}
with patch.object(app_module.db.session, 'get', side_effect=lambda model, user_id: users.get(user_id)):
    assert app.login_manager._user_callback('1') is users[1]
    assert app.login_manager._user_callback('2') is None
print('ok')
"""
    result = subprocess.run(["python", "-c", script], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr


def test_login_manager_is_instantiated_once_and_reimport_is_stable():
    source = Path("app.py").read_text(encoding="utf-8")
    assert source.count("LoginManager()") == 1
    env = os.environ.copy()
    env.update(CODEX_ENV="dev", DATABASE_URL="sqlite:///:memory:", SESSION_SECRET="test-secret")
    script = """
import importlib
import app
first = app.app.login_manager._user_callback
routes = tuple(sorted(app.app.view_functions))
reloaded = importlib.reload(app)
assert reloaded.app.login_manager._user_callback is not None
assert tuple(sorted(reloaded.app.view_functions)) == routes
assert len(reloaded.app.url_map._rules_by_endpoint['user.profile']) == 1
print('ok')
"""
    result = subprocess.run(["python", "-c", script], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr
