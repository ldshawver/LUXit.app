-- Data repair: email opt-outs manufactured by contact_dedupe merge escalation.
--
-- Defect: has_explicit_email_opt_out() treated the Google-import default
-- is_subscribed=false as an email opt-out; the 2026-08-30 03:00 UTC dedupe
-- batch then ran apply_email_opt_out() on 6 merge survivors.
--
-- Evidence per row (see fix/email-suppression-integrity report):
--   * 2026-08-25 07:48 backup: every row below had email_consent_status
--     'unknown' and no email opt-out flag (no contact in prod had one).
--   * Each was merged-into on 2026-08-30 03:00 from a google_contacts
--     duplicate whose only "signal" was is_subscribed=false.
--   * No marketing_audit_log email/do_not_* action, no marketing_preferences_*
--     provenance, no operator unsubscribe / preferences request in nginx logs
--     since 2026-05-27, no bounce/complaint tracking exists.
--
-- Restores the exact pre-merge values. Any row that no longer matches the
-- escalation fingerprint is left untouched (and the counts check aborts).
--
-- Dry run (default, rolls back):  psql "$URL" -f this.sql
-- Apply:                           psql "$URL" -v apply=1 -f this.sql

\set ON_ERROR_STOP on
\if :{?apply}
\else
\set apply 0
\endif

BEGIN;

CREATE TABLE IF NOT EXISTS backup_20261006_email_optout_repair AS
SELECT * FROM contact WHERE false;

-- (contact_id, company_id, is_subscribed before the merge)
CREATE TEMP TABLE repair_target(contact_id int, company_id int, pre_is_subscribed boolean) ON COMMIT DROP;
INSERT INTO repair_target VALUES
  (1,    1, true),
  (934,  1, false),
  (1012, 1, false),
  (1738, 1, false),
  (1878, 1, false),
  (1982, 1, false);

CREATE TEMP TABLE repair_rows ON COMMIT DROP AS
SELECT c.*, t.pre_is_subscribed
FROM contact c
JOIN repair_target t ON t.contact_id = c.id AND t.company_id = c.company_id
WHERE c.do_not_email
  AND c.email_unsubscribed
  AND c.email_subscribed = false
  AND c.is_subscribed = false
  AND c.email_opt_in = false
  AND c.email_consent_status = 'unsubscribed'
  AND c.marketing_preferences_source IS NULL
  AND c.marketing_preferences_updated_at IS NULL
  -- is_subscribed is also the legacy SMS gate: never restore it (or touch the
  -- row at all) once any SMS STOP / opt-out evidence exists on the contact.
  AND NOT c.do_not_sms
  AND NOT c.sms_opted_out
  AND c.sms_opt_out_at IS NULL
  AND coalesce(c.tags, '') !~* 'sms_opt_out'
  AND NOT EXISTS (
        SELECT 1 FROM marketing_audit_log a
        WHERE a.entity_type = 'contact' AND a.entity_id = c.id
          AND a.action ~ '(email|do_not|unsub)')
  AND EXISTS (
        SELECT 1 FROM contact d
        WHERE d.merged_into_contact_id = c.id
          AND d.company_id = c.company_id
          AND d.source = 'google_contacts'
          AND d.is_subscribed = false
          AND NOT d.do_not_email AND NOT d.email_unsubscribed
          AND d.merged_at >= '2026-08-30 03:00' AND d.merged_at < '2026-08-30 03:01');

SELECT id, company_id, (email IS NOT NULL) AS has_email, is_subscribed, pre_is_subscribed FROM repair_rows ORDER BY id;

DO $$
BEGIN
  IF (SELECT count(*) FROM repair_rows) <> 6 THEN
    RAISE EXCEPTION 'expected 6 rows matching the escalation fingerprint, found %',
      (SELECT count(*) FROM repair_rows);
  END IF;
END $$;

INSERT INTO backup_20261006_email_optout_repair
SELECT c.* FROM contact c JOIN repair_rows r ON r.id = c.id;

INSERT INTO marketing_audit_log (company_id, created_by_user_id, entity_type, entity_id, action, details)
SELECT r.company_id, NULL, 'contact', r.id, 'email_optout_repair_merge_escalation',
       jsonb_build_object(
         'reason', 'email opt-out manufactured by contact_dedupe merge of google_contacts default is_subscribed=false (2026-08-30 03:00); no opt-out provenance',
         'backup_table', 'backup_20261006_email_optout_repair',
         'before', jsonb_build_object('do_not_email', r.do_not_email, 'email_unsubscribed', r.email_unsubscribed,
                                      'email_subscribed', r.email_subscribed, 'is_subscribed', r.is_subscribed,
                                      'email_consent_status', r.email_consent_status),
         'after',  jsonb_build_object('do_not_email', false, 'email_unsubscribed', false,
                                      'email_subscribed', true, 'is_subscribed', r.pre_is_subscribed,
                                      'email_consent_status', 'unknown'))
FROM repair_rows r;

-- SMS, do_not_market and do_not_contact fields are deliberately not touched.
UPDATE contact c
SET do_not_email = false,
    email_unsubscribed = false,
    email_subscribed = true,
    is_subscribed = r.pre_is_subscribed,
    email_consent_status = 'unknown'
FROM repair_rows r
WHERE c.id = r.id AND c.company_id = r.company_id;

SELECT id, do_not_email, email_unsubscribed, email_subscribed, is_subscribed, email_consent_status,
       do_not_sms, sms_opted_out, sms_consent_status
FROM contact WHERE id IN (SELECT id FROM repair_rows) ORDER BY id;

\if :apply
COMMIT;
\else
ROLLBACK;
\endif
