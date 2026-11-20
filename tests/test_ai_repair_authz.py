"""Authorization regression tests for the AI repair / code-execution surface.

Until 2026-10-20 these routes were login_required only: any signed-in user
(even a tenant viewer) could append code to any file via
/api/ai/execute-action ADD_FEATURE (root RCE), copy platform secrets into a
tenant, run auto-repair / AI code fixers, and read host logs and source.

Every service the routes call is replaced by a recorder, so a denied request
proves the service was never reached. OpenAI is never contacted.
"""
import json

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from extensions import db
from models import Company, User, UserCompanyAccess

# Request-side claims of admin rights. None of them may count.
SPOOF_JSON = {"is_admin": True, "role": "platform_admin", "admin": True, "user": {"is_admin": True}}
SPOOF_QUERY = "?is_admin=1&role=super_admin&admin=true"
SPOOF_HEADERS = {"X-Is-Admin": "true", "X-User-Role": "platform_admin", "X-Forwarded-For": "127.0.0.1",
                 "X-Real-IP": "127.0.0.1"}

# (method, path, routes-module attribute, service method) for every gated route.
RESTRICTED = [
    ("GET", "/api/diagnostics/errors", "ApplicationDiagnostics", "get_recent_errors"),
    ("GET", "/api/diagnostics/health", "ApplicationDiagnostics", "get_system_health"),
    ("POST", "/api/auto-repair/start", "AutoRepairService", "execute_auto_repair"),
    ("POST", "/api/auto-repair/clear", "AutoRepairService", "clear_resolved_errors"),
    ("GET", "/api/system/diagnosis", "ErrorFixService", "comprehensive_system_diagnosis"),
    ("GET", "/api/system/health", "ErrorFixService", "check_server_health"),
    ("GET", "/api/system/validate-openai", "ErrorFixService", "validate_openai_api_key"),
    ("GET", "/api/system/endpoint-check", "ErrorFixService", "check_404_endpoints"),
    ("POST", "/api/ai-fixer/auto-fix-all", "AICodeFixer", "auto_fix_all_errors"),
    ("POST", "/api/ai-fixer/fix-error/1", "AICodeFixer", "generate_and_apply_fix"),
    ("GET", "/api/ai-fixer/codebase-structure", "AICodeFixer", "get_codebase_structure"),
    ("POST", "/api/ai/execute-action", "AIActionExecutor", "handle_action"),
]

AGENT_ENDPOINTS = [
    ("POST", "/ai-dashboard/execute/app_intelligence"),
    ("POST", "/api/agents/trigger/app_intelligence"),
    ("GET", "/agent/diagnostics"),
    ("POST", "/agent/fix-issues"),
]

NON_ADMINS = ["owner_a", "viewer_a", "owner_b", "spoofer"]


class FakeAgent:
    agent_name = "Fake"

    def __init__(self, calls, name):
        self.calls, self.name = calls, name

    def _hit(self, what):
        self.calls.append((self.name, what))
        return {"success": True}

    def create_task(self, *a, **k):
        return None

    def complete_task(self, *a, **k):
        return None

    def execute(self, task_data):
        return self._hit("execute")

    def auto_detect_and_fix_issues(self):
        return self._hit("auto_fix")

    def read_live_error_logs(self):
        return self._hit("read_logs")

    def get_app_runtime_state(self):
        return self._hit("runtime_state")

    def read_app_files(self, path):
        return self._hit("read_files")

    def perform_health_check(self, _):
        return self._hit("health")


class FakeScheduler:
    def __init__(self, calls):
        self.agents = {"app_intelligence": FakeAgent(calls, "app_intelligence"),
                       "brand_strategy": FakeAgent(calls, "brand_strategy")}


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGNOSTICS_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.delenv("REPL_ID", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-platform-env")

    class _NoOpenAI:
        def __init__(self, *a, **k):
            raise AssertionError("OpenAI must not be contacted")
    monkeypatch.setattr("openai.OpenAI", _NoOpenAI)

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test-secret", SERVER_NAME="localhost")

    import routes
    calls = []
    for _, _, attr, meth in RESTRICTED:
        target = getattr(routes, attr)
        assert target is not None, attr
        monkeypatch.setattr(target, meth,
                            staticmethod(lambda *a, _m=meth, **k: calls.append(_m) or {"status": "ok"}))
    monkeypatch.setattr(routes.ApplicationDiagnostics, "get_error_summary", staticmethod(lambda *a, **k: {}))
    agent_calls = []
    scheduler = FakeScheduler(agent_calls)
    monkeypatch.setattr("agent_scheduler.get_agent_scheduler", lambda: scheduler)

    with app.app_context():
        db.create_all()
        a, b = Company(name="Tenant A"), Company(name="Tenant B")
        db.session.add_all([a, b])
        db.session.flush()

        def mk(username, company, role, is_admin=False, **profile):
            u = User(username=username, email=f"{username}@example.com", is_admin=is_admin,
                     default_company_id=company.id, password_hash=generate_password_hash("pw-123456"), **profile)
            db.session.add(u)
            db.session.flush()
            db.session.add(UserCompanyAccess(user_id=u.id, company_id=company.id, role=role, is_default=True))
            return u

        platform = mk("platform", a, "owner", is_admin=True)
        owner_a = mk("owner-a", a, "owner")
        viewer_a = mk("viewer-a", a, "viewer")
        owner_b = mk("owner-b", b, "owner")
        spoofer = mk("spoofer", a, "viewer", segment="super_admin", tags="platform_owner,system_admin,is_admin")
        db.session.commit()
        ids = dict(a=a.id, b=b.id, platform=platform.id, owner_a=owner_a.id, viewer_a=viewer_a.id,
                   owner_b=owner_b.id, spoofer=spoofer.id)

        def client_for(user_id):
            c = app.test_client()
            if user_id is not None:
                with c.session_transaction() as sess:
                    sess["_user_id"] = str(user_id)
                    sess["_fresh"] = True
            return c

        yield app, client_for, ids, calls, agent_calls
        db.session.remove()
        db.drop_all()


def _call(client, method, path, spoof=True):
    if not spoof:
        return client.open(path, method=method, data=json.dumps({"action": "GET_CODEBASE_INFO"}),
                           content_type="application/json")
    body = dict(SPOOF_JSON, action="ADD_FEATURE", task_type="monitor_and_fix",
                task_data={"task_type": "read_files", "file_path": "/etc/shadow"},
                params={"feature_name": "x", "code": "import os", "file_path": "/etc/cron.d/x"})
    return client.open(path + SPOOF_QUERY, method=method, data=json.dumps(body),
                       content_type="application/json", headers=SPOOF_HEADERS)


@pytest.mark.parametrize("who", NON_ADMINS)
@pytest.mark.parametrize("method,path,attr,meth", RESTRICTED)
def test_non_admin_cannot_reach_repair_or_code_execution_routes(world, who, method, path, attr, meth):
    app, client_for, ids, calls, _ = world
    resp = _call(client_for(ids[who]), method, path)
    assert resp.status_code == 403
    assert calls == []


@pytest.mark.parametrize("method,path,attr,meth", RESTRICTED)
def test_anonymous_cannot_reach_repair_or_code_execution_routes(world, method, path, attr, meth):
    app, client_for, ids, calls, _ = world
    resp = _call(client_for(None), method, path)
    assert resp.status_code in (302, 401, 403)
    assert calls == []


@pytest.mark.parametrize("who", NON_ADMINS + [None])
@pytest.mark.parametrize("method,path", AGENT_ENDPOINTS)
def test_non_admin_cannot_drive_app_intelligence_agent_via_any_endpoint(world, who, method, path):
    app, client_for, ids, _, agent_calls = world
    resp = _call(client_for(ids.get(who)), method, path)
    assert resp.status_code in ((302, 401, 403) if who is None else (403,))
    assert agent_calls == []


@pytest.mark.parametrize("method,path,attr,meth", RESTRICTED)
def test_platform_admin_keeps_access(world, method, path, attr, meth):
    app, client_for, ids, calls, _ = world
    resp = _call(client_for(ids["platform"]), method, path, spoof=False)
    if path == "/api/ai-fixer/fix-error/1":
        assert resp.status_code == 404  # authorised; no such error row
    else:
        assert resp.status_code == 200
        assert calls == [meth]


@pytest.mark.parametrize("method,path", AGENT_ENDPOINTS)
def test_platform_admin_can_drive_app_intelligence_agent(world, method, path):
    app, client_for, ids, _, agent_calls = world
    resp = _call(client_for(ids["platform"]), method, path, spoof=False)
    assert resp.status_code == 200
    assert agent_calls


@pytest.mark.parametrize("path", ["/ai-dashboard/execute/brand_strategy", "/api/agents/trigger/brand_strategy"])
def test_tenant_marketing_agents_are_not_over_blocked(world, path):
    app, client_for, ids, _, agent_calls = world
    resp = client_for(ids["viewer_a"]).post(path, data=json.dumps({"task_type": "x", "task_data": {}}),
                                            content_type="application/json")
    assert resp.status_code == 200
    assert agent_calls == [("brand_strategy", "execute")]


def test_add_feature_is_not_dispatchable_even_for_platform_admin(tmp_path):
    from ai_action_executor import AIActionExecutor
    target = tmp_path / "payload.py"
    result = AIActionExecutor.handle_action("ADD_FEATURE", {"feature_name": "x", "code": "import os",
                                                            "file_path": str(target)})
    assert "Unknown action" in result.get("error", "")
    assert not target.exists()


def test_code_writing_and_secret_copy_primitives_are_gone():
    import ai_action_executor
    import ai_code_fixer
    import advanced_config
    assert not hasattr(ai_action_executor.AIActionExecutor, "add_feature_code")
    assert not hasattr(ai_action_executor.AIActionExecutor, "populate_company_secrets")
    assert not hasattr(ai_code_fixer.AICodeFixer, "write_code_fix")
    assert not hasattr(advanced_config, "migrate_env_secrets")


def test_populate_secrets_is_not_dispatchable_even_for_platform_admin(monkeypatch):
    from ai_action_executor import AIActionExecutor
    from models import Company
    monkeypatch.setattr(Company, "set_secret", lambda *a, **k: pytest.fail("platform secret copied"))
    result = AIActionExecutor.handle_action("POPULATE_SECRETS", {"company_name": "Tenant A"})
    assert "Unknown action" in result.get("error", "")


@pytest.mark.parametrize("who", NON_ADMINS + ["platform"])
def test_nobody_can_copy_platform_env_secrets_into_a_company(world, who):
    app, client_for, ids, _, _ = world
    company = ids["b"] if who == "owner_b" else ids["a"]
    resp = client_for(ids[who]).post(f"/api/migrate-env-secrets/{company}", data=json.dumps(SPOOF_JSON),
                                     content_type="application/json", headers=SPOOF_HEADERS)
    assert resp.status_code == 404
    from models import CompanyIntegrationConfig
    with app.app_context():
        assert CompanyIntegrationConfig.query.count() == 0


@pytest.mark.parametrize("who", NON_ADMINS + [None])
def test_debug_session_ignores_spoofed_forwarded_for(world, who):
    app, client_for, ids, _, _ = world
    resp = client_for(ids.get(who)).get("/auth/debug-session", headers=SPOOF_HEADERS)
    assert resp.status_code == 404


def test_debug_session_for_platform_admin_omits_secret_key(world):
    app, client_for, ids, _, _ = world
    resp = client_for(ids["platform"]).get("/auth/debug-session")
    assert resp.status_code == 200
    assert "secret_key_prefix" not in resp.get_json()
    assert "test-sec" not in resp.get_data(as_text=True)
