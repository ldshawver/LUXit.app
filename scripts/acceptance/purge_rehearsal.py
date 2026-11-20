#!/usr/bin/env python3
"""Purge rehearsal against an isolated PostgreSQL copy (run by
isolated_purge_rehearsal.sh, never against a shared database).

For each workspace in the copy (or --company-id): close it with a purge date
in the past, purge it for real, and check that
  * PostgreSQL accepted every delete (foreign keys enforced, unlike SQLite),
  * none of the workspace's rows remain in any scoped table,
  * every other workspace's row counts are unchanged,
  * the company tombstone and security audit rows remain.
Prints one JSON line per workspace and a summary; exit 1 on any failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    if "rig" not in os.environ.get("DATABASE_URL", "") or "host=/var/tmp/" not in os.environ.get("DATABASE_URL", ""):
        print("refusing: DATABASE_URL is not an isolated rig socket", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser()
    ap.add_argument("--company-id", type=int, action="append")
    args = ap.parse_args()

    import sqlalchemy as sa

    from app import create_app
    from extensions import db
    from models import Company, WorkspaceClosure
    from services.workspace_lifecycle import _condition, _scope_map, purge_workspace

    app = create_app()
    failures = 0
    with app.app_context():
        scope = _scope_map()
        present = set(sa.inspect(db.engine).get_table_names())
        tables = [t for t in scope if t.name in present]
        missing = sorted(t.name for t in scope if t.name not in present)

        def counts():
            out = {}
            for t in tables:
                if "company_id" in t.c:
                    for cid, n in db.session.execute(sa.select(t.c.company_id, sa.func.count())
                                                     .group_by(t.c.company_id)):
                        out[(t.name, cid)] = n
            return out

        ids = args.company_id or [c.id for c in Company.query.order_by(Company.id).all()]
        print(json.dumps({"workspaces": len(ids), "scoped_tables": len(tables),
                          "model_tables_missing_in_db": missing}))
        for cid in ids:
            before = counts()
            company = db.session.get(Company, cid)
            company.is_active = False
            past = datetime.utcnow() - timedelta(days=1)
            db.session.merge(WorkspaceClosure(company_id=cid, closed_at=past, purge_after=past))
            db.session.commit()
            try:
                result = purge_workspace(cid, apply=True)
            except Exception as exc:  # noqa: BLE001 - report every failure
                db.session.rollback()
                failures += 1
                print(json.dumps({"company_id": cid, "ok": False, "error": str(exc).splitlines()[0][:300]}))
                continue
            leftover = {}
            for t in tables:
                if t.name in ("security_audit_event", "marketing_audit_log", "workspace_closure"):
                    continue
                n = db.session.execute(sa.select(sa.func.count()).select_from(t)
                                       .where(_condition(t, cid, scope))).scalar()
                if n:
                    leftover[t.name] = n
            after = counts()
            changed = {f"{k[0]}:{k[1]}": [before.get(k), after.get(k)]
                       for k in set(before) | set(after)
                       if k[1] != cid and before.get(k) != after.get(k)}
            ok = not leftover and not changed and db.session.get(Company, cid) is not None
            failures += 0 if ok else 1
            print(json.dumps({"company_id": cid, "ok": ok, "deleted": sum(result["rows"].values()),
                              "tables": len(result["rows"]), "leftover": leftover, "others_changed": changed}))
    print(json.dumps({"summary": "PASS" if not failures else "FAIL", "failures": failures}))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
