from pathlib import Path


def test_production_workflow_runs_ledger_migrations_before_restart():
    workflow = Path(".github/workflows/push-to-production.yml").read_text(encoding="utf-8")
    assert 'test -n "${DATABASE_URL:-}"' in workflow
    migration_pos = workflow.index('scripts/apply_migrations.sh "$DATABASE_URL" migrations')
    syntax_pos = workflow.index('echo "== Python syntax check =="')
    restart_pos = workflow.index('echo "== Restart live service =="')
    assert migration_pos < syntax_pos < restart_pos
    assert 'set -euo pipefail' in workflow
    assert 'script_stop: true' in workflow


def test_migration_runner_uses_advisory_lock_transactions_and_safe_ledger():
    runner = Path("scripts/apply_migrations.py").read_text(encoding="utf-8")
    assert "pg_try_advisory_lock" in runner
    assert "Timed out waiting for migration advisory lock" in runner
    assert "ensure_ledger(conn, table_ident)" in runner
    assert "applied_checksum(conn, table_ident, path.name)" in runner
    assert '["--single-transaction"]' in runner
    assert "INCOMPATIBLE_TRANSACTION_RE" in runner
    assert "BEGIN" in runner and "COMMIT" in runner
    assert '"psql", args.database_url, "-v", "ON_ERROR_STOP=1", *flags, "-f", str(path)' in runner
    assert "duration_ms integer NOT NULL DEFAULT 0" in runner
    assert "deployment_id text NULL" in runner
    assert "VALUES (%s, %s, %s, %s, current_user, %s)" in runner
    assert "VALID_NAME" in runner
    assert "Migration symlink escapes migrations directory" in runner
    assert "hashlib.sha256" in runner
    audit = Path("docs/migration_transaction_audit.md").read_text(encoding="utf-8")
    assert "20260714_contact_intelligence_crm.sql" in audit
    assert "Safe with `--single-transaction`" in audit
    assert "Already transaction wrapped" in audit


def test_migration_bootstrap_is_separate_dry_run_first_and_refuses_uncertain_seed():
    bootstrap = Path("scripts/bootstrap_migration_ledger.py").read_text(encoding="utf-8")
    assert "readonly=not args.confirm" in bootstrap
    assert "cannot_determine_automatically" in bootstrap
    assert "Refusing to seed ledger" in bootstrap
    assert "proven_applied" in bootstrap
    assert "application_commit" in bootstrap
    assert "database_identity" in bootstrap


def test_user_archive_migration_is_forward_safe_and_before_restart():
    workflow = Path(".github/workflows/push-to-production.yml").read_text(encoding="utf-8")
    migration_pos = workflow.index('scripts/apply_migrations.sh "$DATABASE_URL" migrations')
    restart_pos = workflow.index('echo "== Restart live service =="')
    assert migration_pos < restart_pos
    sql = Path("migrations/20260718_user_archive_restore.sql").read_text(encoding="utf-8")
    required = [
        'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE',
        'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP NULL',
        'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS archived_by_user_id INTEGER NULL REFERENCES "user"(id) ON DELETE SET NULL',
        'UPDATE "user" SET active = TRUE WHERE active IS NULL',
    ]
    for fragment in required:
        assert fragment in sql


def test_production_deploy_targets_canonical_service_port_healthz_and_current_logs():
    workflow = Path(".github/workflows/push-to-production.yml").read_text(encoding="utf-8")
    assert 'SERVICE="lux-email-bot.service"' in workflow
    assert 'PORT="8001"' in workflow
    assert 'http://127.0.0.1:${PORT}/healthz' in workflow
    assert 'https://luxit.app/healthz' in workflow
    assert 'restart_since="$(date --utc +%Y-%m-%dT%H:%M:%SZ)"' in workflow
    assert 'journalctl -u "$SERVICE" --since "$restart_since"' in workflow
    assert 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/verify_production_schema.sql' in workflow
    assert workflow.index('scripts/apply_migrations.sh') < workflow.index('verify_production_schema.sql') < workflow.index('systemctl restart "$SERVICE"')
    assert 'systemctl restart luxit.service' not in workflow


def test_deploy_script_documents_legacy_duplicate_and_uses_ledger():
    deploy = Path("deploy.sh").read_text(encoding="utf-8")
    assert 'SERVICE="lux-email-bot.service"' in deploy
    assert 'BIND="127.0.0.1:8001"' in deploy
    assert 'luxit.service/8000 is legacy' in deploy
    assert 'scripts/apply_migrations.sh' in deploy
    assert 'http://$BIND/healthz' in deploy
    assert 'verify_production_schema.sql' in deploy
    notes = Path("docs/production_deployment_notes.md").read_text(encoding="utf-8")
    assert "SECRET_KEY" in notes and "remember cookies" in notes
    assert "lux-email-bot.service" in notes and "luxit.service" in notes


def test_user_archive_migration_discovered_by_runner_ordering():
    from scripts.apply_migrations import discover_migrations
    names = [p.name for p in discover_migrations(Path("migrations"))]
    assert "20260718_user_archive_restore.sql" in names
    assert names == sorted(names)
    assert "20260718_audience_schema_repair.sql" in names


def test_schema_verification_covers_audience_and_user_archive():
    sql = Path("scripts/verify_production_schema.sql").read_text(encoding="utf-8")
    for required in (
        "contact_phone_number", "contact_email_address", "contact_source_event",
        "google_contact_connection", "opportunity", "contact_task", "segment_member",
        "display_name", "updated_at", "session_revoked_at", "previous_role",
    ):
        assert required in sql
    assert "RAISE EXCEPTION" in sql
