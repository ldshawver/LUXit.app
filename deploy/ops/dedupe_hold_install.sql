-- Code-independent hold for the weekly_contact_dedupe APScheduler job.
-- APScheduler's SQLAlchemyJobStore selects due work ONLY via the next_run_time
-- column (get_due_jobs: next_run_time <= now; wakeups: min non-NULL next_run_time).
-- While this trigger exists:
--   * an UPDATE of an already-paused row (startup add_job(replace_existing=True),
--     resume_job, modify_job) is turned into a no-op: column AND pickled state kept;
--   * any INSERT / UPDATE of this id is forced to next_run_time = NULL (never due).
-- Survives worker restart, crash and host reboot, for ANY worker code version.
-- Release: run dedupe_hold_release.sql (DROP TRIGGER + FUNCTION).
BEGIN;
CREATE OR REPLACE FUNCTION luxit_ops_hold_weekly_contact_dedupe() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id = 'weekly_contact_dedupe' THEN
    IF TG_OP = 'UPDATE' AND OLD.next_run_time IS NULL THEN
      RETURN OLD;
    END IF;
    NEW.next_run_time := NULL;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS luxit_ops_hold_weekly_contact_dedupe ON apscheduler_jobs;
CREATE TRIGGER luxit_ops_hold_weekly_contact_dedupe
  BEFORE INSERT OR UPDATE ON apscheduler_jobs
  FOR EACH ROW EXECUTE FUNCTION luxit_ops_hold_weekly_contact_dedupe();
COMMENT ON FUNCTION luxit_ops_hold_weekly_contact_dedupe() IS
  'OPS HOLD 2026-10: keeps weekly_contact_dedupe paused across worker restarts. Release via dedupe_hold_release.sql.';
COMMIT;
