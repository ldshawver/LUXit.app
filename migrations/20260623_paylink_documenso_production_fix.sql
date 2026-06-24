BEGIN;
-- Scope note: these preference columns are compatibility fixes for production
-- schema drift encountered by the PayLink/Documenso signing notification path.
-- They are not a product-level PWA/push/after-hours feature completion claim.
ALTER TABLE IF EXISTS pwa_device ADD COLUMN IF NOT EXISTS push_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE IF EXISTS "user" ADD COLUMN IF NOT EXISTS pwa_after_hours_push_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE IF EXISTS contractor_contracts ADD COLUMN IF NOT EXISTS documenso_document_id TEXT;
ALTER TABLE IF EXISTS contractor_contracts ADD COLUMN IF NOT EXISTS documenso_signing_url TEXT;
ALTER TABLE IF EXISTS contractor_contracts ADD COLUMN IF NOT EXISTS signature_status TEXT;
ALTER TABLE IF EXISTS contract_signers ADD COLUMN IF NOT EXISTS documenso_recipient_id TEXT;
ALTER TABLE IF EXISTS contract_signers ADD COLUMN IF NOT EXISTS signing_url TEXT;
ALTER TABLE IF EXISTS contract_signers ADD COLUMN IF NOT EXISTS status TEXT;
CREATE TABLE IF NOT EXISTS documenso_signature_requests (
  id BIGSERIAL PRIMARY KEY,
  contract_id UUID NOT NULL,
  documenso_document_id TEXT NOT NULL,
  signing_url TEXT,
  recipient_id TEXT,
  status TEXT NOT NULL DEFAULT 'sent',
  response_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_documenso_signature_requests_contract_doc_recipient
  ON documenso_signature_requests (contract_id, documenso_document_id, COALESCE(recipient_id, ''));
CREATE TABLE IF NOT EXISTS documenso_webhook_events (
  id BIGSERIAL PRIMARY KEY,
  event_id TEXT UNIQUE,
  documenso_document_id TEXT,
  event_type TEXT,
  verified BOOLEAN NOT NULL DEFAULT FALSE,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  processed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMIT;
-- Rollback notes: drop only columns/index/tables created above after confirming no production Documenso data is needed.
