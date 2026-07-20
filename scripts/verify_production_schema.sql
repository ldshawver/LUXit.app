DO $$
DECLARE
  missing text;
BEGIN
  WITH required(table_name, column_name) AS (VALUES
    ('contact', 'company_id'), ('contact', 'is_active'), ('contact', 'archived_at'),
    ('contact', 'display_name'), ('contact', 'original_source'),
    ('contact', 'google_match_status'), ('contact', 'updated_at'),
    ('user', 'active'), ('user', 'archived_at'), ('user', 'archived_by_user_id'),
    ('user', 'archived_company_id'), ('user', 'session_revoked_at'),
    ('user_company_access', 'is_active'), ('user_company_access', 'archived_at'),
    ('user_company_access', 'archived_by_user_id'), ('user_company_access', 'previous_role')
  )
  SELECT string_agg(format('%I.%I', required.table_name, required.column_name), ', ')
    INTO missing
  FROM required
  LEFT JOIN information_schema.columns c
    ON c.table_schema = 'public'
   AND c.table_name = required.table_name
   AND c.column_name = required.column_name
  WHERE c.column_name IS NULL;

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'Required production columns are missing: %', missing;
  END IF;

  SELECT string_agg(name, ', ')
    INTO missing
  FROM unnest(ARRAY[
    'contact_phone_number', 'contact_email_address', 'contact_source_event',
    'google_contact_connection', 'opportunity', 'contact_task', 'segment', 'segment_member'
  ]) AS name
  WHERE to_regclass('public.' || name) IS NULL;

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'Required Audience tables are missing: %', missing;
  END IF;
END $$;

