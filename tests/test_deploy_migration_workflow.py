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


def test_migration_script_orders_fails_fast_and_records_ledger():
    script = Path("scripts/apply_migrations.sh").read_text(encoding="utf-8")
    assert 'set -euo pipefail' in script
    assert 'CREATE TABLE IF NOT EXISTS ${LEDGER_TABLE}' in script
    assert 'find "$MIGRATIONS_DIR" -maxdepth 1 -type f -name "*.sql" ! -iname "*rollback*" | sort | while read -r f' in script
    assert 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"' in script
    assert 'INSERT INTO ${LEDGER_TABLE} (filename, checksum)' in script
    assert 'Migration checksum changed after application' in script
    assert script.index('psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"') < script.index('INSERT INTO ${LEDGER_TABLE} (filename, checksum)')
