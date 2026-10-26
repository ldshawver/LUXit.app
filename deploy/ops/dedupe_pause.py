"""Pause weekly_contact_dedupe in the APScheduler job store and (optionally)
install the code-independent hold trigger, in one transaction.

    python dedupe_pause.py --dsn-env-file /etc/luxit/prod.env            # dry run
    python dedupe_pause.py --dsn-env-file /etc/luxit/prod.env --apply --hold

Pause = APScheduler's own semantics: next_run_time NULL in the column AND in the
pickled job_state. The original row is written to --backup-dir first. The hold
trigger (deploy/ops/dedupe_hold_install.sql) keeps it paused across worker
restarts/reboots; without it, any worker restart re-arms the job.
"""
import argparse, json, os, pathlib, pickle, sys
from datetime import datetime, timezone

import psycopg2

JOB_ID = "weekly_contact_dedupe"
HERE = pathlib.Path(__file__).resolve().parent


def dsn_from(env_file):
    for line in pathlib.Path(env_file).read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("DATABASE_URL not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn-env-file", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--hold", action="store_true", help="also install the hold trigger")
    ap.add_argument("--backup-dir", default="/root/luxit-ops-backups")
    a = ap.parse_args()

    conn = psycopg2.connect(dsn_from(a.dsn_env_file))
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SELECT next_run_time, job_state FROM apscheduler_jobs WHERE id=%s FOR UPDATE", (JOB_ID,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"{JOB_ID} not found")
    nrt, state_bytes = row[0], bytes(row[1])
    state = pickle.loads(state_bytes)
    human = datetime.fromtimestamp(nrt, timezone.utc).isoformat() if nrt else None
    print(f"current: column next_run_time={nrt} ({human}); pickled next_run_time={state.get('next_run_time')}")
    cur.execute("SELECT 1 FROM pg_trigger WHERE tgname='luxit_ops_hold_weekly_contact_dedupe'")
    print(f"hold trigger installed: {bool(cur.fetchone())}")
    if nrt is None and state.get("next_run_time") is None:
        print("already paused")
    if not a.apply:
        print("dry run: no changes")
        conn.rollback()
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bdir = pathlib.Path(a.backup_dir) / f"{stamp}-dedupe-pause"
    bdir.mkdir(parents=True, exist_ok=False)
    (bdir / f"{JOB_ID}.job_state.orig.pickle").write_bytes(state_bytes)
    (bdir / f"{JOB_ID}.next_run_time.orig.json").write_text(json.dumps({"next_run_time": nrt, "utc": human}))
    os.chmod(bdir, 0o700)

    state["next_run_time"] = None
    cur.execute("UPDATE apscheduler_jobs SET next_run_time=NULL, job_state=%s WHERE id=%s",
                (psycopg2.Binary(pickle.dumps(state, pickle.HIGHEST_PROTOCOL)), JOB_ID))
    if a.hold:
        cur.execute((HERE / "dedupe_hold_install.sql").read_text().replace("BEGIN;", "").replace("COMMIT;", ""))
    conn.commit()
    print(f"paused{' + hold installed' if a.hold else ''}; original saved in {bdir}")


if __name__ == "__main__":
    main()
