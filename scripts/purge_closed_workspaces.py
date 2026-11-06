#!/usr/bin/env python3
"""Delete the data of closed workspaces whose retention period has passed.

Dry run by default: lists each due workspace and the rows that would be
deleted. ``--apply`` deletes them (one transaction per workspace) and keeps
the company row as a tombstone plus the security audit trail.

    python scripts/purge_closed_workspaces.py                 # what is due
    python scripts/purge_closed_workspaces.py --company-id 42  # one workspace
    python scripts/purge_closed_workspaces.py --apply          # delete

Runs against whatever DATABASE_URL the app config resolves; take a backup
first (deploy/backup/) and run it on staging before production.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    parser.add_argument("--company-id", type=int, help="only this workspace")
    args = parser.parse_args(argv)

    from app import create_app
    from services.workspace_lifecycle import ClosureError, due_for_purge, purge_workspace

    app = create_app()
    with app.app_context():
        ids = [args.company_id] if args.company_id else [c.company_id for c in due_for_purge()]
        if not ids:
            print(json.dumps({"due": 0}))
            return 0
        failed = 0
        for company_id in ids:
            try:
                result = purge_workspace(company_id, apply=args.apply)
            except ClosureError as exc:
                failed += 1
                result = {"company_id": company_id, "refused": exc.code, "message": exc.message}
            print(json.dumps(result, sort_keys=True))
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
