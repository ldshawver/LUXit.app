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


def test_crm_repair_migration_is_idempotent_backfills_and_preserves_records():
    sql = Path("migrations/20260719_crm_contact_management_repair.sql").read_text(encoding="utf-8")
    assert sql.count("ADD COLUMN IF NOT EXISTS") >= 20
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()
    assert 'UPDATE "user" SET active = TRUE WHERE active IS NULL' in sql
    assert "UPDATE contact SET tenant_id = company_id" in sql
    assert "UPDATE contact SET status = CASE" in sql
    for table in ("contact_phone_number", "contact_email_address", "contact_source_event", "opportunity"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_contact_company_normalized_phone" in sql


def test_deploy_verifies_crm_schema_after_migrations_and_before_restart():
    workflow = Path(".github/workflows/push-to-production.yml").read_text(encoding="utf-8")
    migration_pos = workflow.index('scripts/apply_migrations.sh "$DATABASE_URL" migrations')
    verification_pos = workflow.index("scripts/verify_crm_schema.py")
    restart_pos = workflow.index('echo "== Restart live service =="')
    assert migration_pos < verification_pos < restart_pos

    verifier = Path("scripts/verify_crm_schema.py").read_text(encoding="utf-8")
    for required in ("user", "contact", "contact_phone_number", "contact_source_event", "opportunity", "twilio_message"):
        assert f'"{required}"' in verifier
    assert "backfill:user.active" in verifier
    assert "backfill:contact.do_not_contact" in verifier
