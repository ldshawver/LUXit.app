BEGIN;
DROP TRIGGER IF EXISTS luxit_ops_hold_weekly_contact_dedupe ON apscheduler_jobs;
DROP FUNCTION IF EXISTS luxit_ops_hold_weekly_contact_dedupe();
COMMIT;
