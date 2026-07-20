-- Forward repair for the Audience schema deployed with the cumulative CRM branch.
-- The original CRM migration omitted contact.updated_at even though the ORM selects it.

ALTER TABLE contact ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;
UPDATE contact SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
WHERE updated_at IS NULL;
ALTER TABLE contact ALTER COLUMN updated_at SET DEFAULT CURRENT_TIMESTAMP;

DO $$
DECLARE
  missing text;
BEGIN
  SELECT string_agg(required.object_name, ', ' ORDER BY required.object_name)
    INTO missing
  FROM (VALUES
    ('column contact.company_id', to_regclass('public.contact') IS NOT NULL AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='contact' AND column_name='company_id')),
    ('column contact.is_active', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='contact' AND column_name='is_active')),
    ('column contact.archived_at', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='contact' AND column_name='archived_at')),
    ('column contact.display_name', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='contact' AND column_name='display_name')),
    ('column contact.updated_at', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='contact' AND column_name='updated_at')),
    ('table contact_phone_number', to_regclass('public.contact_phone_number') IS NOT NULL),
    ('table contact_email_address', to_regclass('public.contact_email_address') IS NOT NULL),
    ('table contact_source_event', to_regclass('public.contact_source_event') IS NOT NULL),
    ('table google_contact_connection', to_regclass('public.google_contact_connection') IS NOT NULL),
    ('table opportunity', to_regclass('public.opportunity') IS NOT NULL),
    ('table contact_task', to_regclass('public.contact_task') IS NOT NULL),
    ('table segment', to_regclass('public.segment') IS NOT NULL),
    ('table segment_member', to_regclass('public.segment_member') IS NOT NULL)
  ) AS required(object_name, present)
  WHERE NOT required.present;

  IF missing IS NOT NULL THEN
    RAISE EXCEPTION 'Audience schema prerequisites are missing: %', missing;
  END IF;
END $$;

