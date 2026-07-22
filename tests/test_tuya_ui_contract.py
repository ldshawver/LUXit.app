"""Static safety contracts remain runnable while the baseline ORM import is blocked."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_hub_contains_authorized_tuya_tab_and_post_only_actions():
    template = (ROOT / "templates/twilio/comms_hub.html").read_text()
    routes = (ROOT / "twilio_sms.py").read_text()
    assert "Tuya Notifications" in template
    assert "can_admin_tuya" in template
    assert 'route("/comms/tuya-notifications/test", methods=["POST"])' in routes
    assert 'route("/comms/tuya-notifications/off", methods=["POST"])' in routes
    assert "csrf_token" in template


def test_page_contract_never_embeds_secret_or_token_values():
    template = (ROOT / "templates/twilio/comms_hub.html").read_text().lower()
    forbidden = (
        "access_token",
        "refresh_token",
        "local_key",
        "access_secret",
        "complete request headers",
    )
    assert not any(value in template for value in forbidden)
    assert "secret_status" in template


def test_ui_uses_cached_unknown_device_state_and_no_live_refresh():
    routes = (ROOT / "twilio_sms.py").read_text()
    template = (ROOT / "templates/twilio/comms_hub.html").read_text()
    assert '"device_state": "unknown"' in routes
    assert (
        "set_switch"
        not in routes[
            routes.index("def comms_hub") : routes.index("def comms_number_permissions")
        ]
    )
    assert (
        "fetch("
        not in template[
            template.index("{% elif tab == 'tuya_notifications' %") : template.index(
                "{% elif tab == 'integrations' %}"
            )
        ]
    )


def test_navigation_is_responsive_and_devices_are_distinguished():
    template = (ROOT / "templates/twilio/comms_hub.html").read_text()
    assert "overflow-x:auto" in template
    assert "separate from the PWA browser devices" in template
    assert "table-responsive" in template


def test_migration_uses_timestamptz_and_tenant_scoped_sid():
    migration = (ROOT / "migrations/20260722_tuya_notification.sql").read_text()
    assert "TIMESTAMPTZ" in migration
    assert "UNIQUE (company_id, message_sid)" in migration
    assert "tuya_notification_worker_heartbeat" in migration


def test_scheduler_uses_stable_module_callable_without_secret_arguments():
    scheduler = (ROOT / "scheduler.py").read_text()
    registration = scheduler[
        scheduler.index("func=run_tuya_reconciliation") : scheduler.index(
            "# Start scheduler"
        )
    ]
    assert "lambda" not in registration
    assert "args=" not in registration
    assert "secret" not in registration.lower()
    assert 'id="tuya_notification_reconciliation"' in registration


def test_deployment_runbook_discovers_forward_only_sql_migrations():
    runbook = (ROOT / "DEPLOYMENT_RUNBOOK.md").read_text()
    assert 'scripts/apply_migrations.py "$DATABASE_URL" migrations' in runbook
