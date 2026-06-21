# LUXit Documenso VPS signing-link runbook

This repository does **not** contain the MyPayLink frontend/backend route that failed:
`https://app.mypaylink.app/app/contractor-hub/contracts/735551c2-ec6c-41e6-976d-1eef4e13bfa5/sign`.
Treat MyPayLink application fixes as out of scope unless a future branch adds the
Documenso integration code that generates MyPayLink signing links.

## Required signing URL policy

Documenso signing-required emails must use one of these explicitly configured URL
sources:

1. **Documenso-hosted signing URL**: preferred default. Public URL variables such
   as `APP_URL`, `NEXT_PUBLIC_WEBAPP_URL`, `NEXTAUTH_URL`, `WEBAPP_URL`, or the
   deployment-specific `DOCUMENSO_PUBLIC_URL` must point to the reachable
   Documenso public host, not MyPayLink.
2. **MyPayLink-owned public token URL**: allowed only when MyPayLink confirms and
   owns the route. Store the URL as metadata/return URL only when needed.
3. **MyPayLink authenticated deep link**: blocked unless explicitly confirmed.
   Do not send `/app/contractor-hub/contracts/:id/sign` from Documenso by
   default; that path is a known-broken signing email target from the incident.

## VPS investigation checklist

Run these on the LUXit/Documenso VPS (Debian 13 Docker Manager host):

```bash
# Identify Documenso containers and compose project.
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'

# Inspect runtime URL/mail/webhook environment without dumping unrelated secrets.
docker inspect <documenso-container> \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | sort \
  | grep -E '^(APP_URL|NEXT_PUBLIC_WEBAPP_URL|NEXTAUTH_URL|WEBAPP_URL|DOCUMENSO_|MAIL_|SMTP_|WEBHOOK_|API_)='

# Inspect compose-time configuration.
find / -name 'docker-compose*.yml' -o -name 'compose*.yaml' 2>/dev/null \
  | xargs -r grep -nE 'APP_URL|NEXT_PUBLIC_WEBAPP_URL|NEXTAUTH_URL|WEBAPP_URL|DOCUMENSO_|WEBHOOK_|MAIL_|SMTP_|contractor-hub'

# Search mounted templates/config for the broken path.
docker exec <documenso-container> sh -lc \
  "grep -R --line-number '/app/contractor-hub/contracts/' /app /data 2>/dev/null || true"
```

## Affected contract/document investigation

Use the incident contract id:
`735551c2-ec6c-41e6-976d-1eef4e13bfa5`.

```bash
# Application logs around document creation/signing email/webhook delivery.
docker logs --since 168h <documenso-container> 2>&1 \
  | grep -E '735551c2-ec6c-41e6-976d-1eef4e13bfa5|signing|email|webhook|redirect|return|metadata|document'

# Database inspection. Adjust container/db/user names to the VPS compose file.
docker exec -it <postgres-container> psql -U <user> -d <database>
```

Inside `psql`, first list Documenso table names because upstream schemas vary by
version:

```sql
\dt
SELECT table_name, column_name
FROM information_schema.columns
WHERE column_name ILIKE '%metadata%'
   OR column_name ILIKE '%redirect%'
   OR column_name ILIKE '%return%'
   OR column_name ILIKE '%sign%'
   OR column_name ILIKE '%webhook%'
ORDER BY table_name, ordinal_position;
```

Then query the document, recipients/signers, webhook/event records, and metadata
for the affected contract id. The exact table names may differ by Documenso
version, but the query target is:

- Documenso document id
- document status and completion timestamp
- signer names/emails/status
- generated signing token/link fields
- redirect/return URL fields
- metadata payload containing the LUXit/MyPayLink contract id
- webhook event delivery status/history
- completed PDF/download reference

## Completion email behavior

Documenso completion email support is deployment/version dependent. Confirm in
Documenso admin settings and email/template settings whether completion emails
are enabled for all required parties. If Documenso cannot deliver completed
agreements in this setup, MyPayLink must handle completed-document emails after a
Documenso completion webhook; do not implement MyPayLink email delivery in this
repository unless this repository owns that integration.

The completion webhook payload must include, or allow MyPayLink to retrieve:

- document id and event id
- document status
- signer names, emails, and status
- completion timestamp
- signed PDF download/reference
- metadata for contract/company/contractor/proposal/invoice ids

## Guard validation

Before sending signing email links, validate that generated links are absolute
HTTPS URLs and are not the known-broken MyPayLink deep link. Emit audit events:

- `signing_link_generated`
- `signing_link_generation_failed`
- `signing_email_sent`
- `signing_email_blocked_invalid_url`

Use `scripts/documenso/verify_documenso_signing_config.py` in CI/deployment to
block checked-in compose/template/config files that contain the broken path or
invalid Documenso public URL settings.

## One-command live audit helper

After copying this repository to the Documenso VPS, run:

```bash
DOCUMENSO_PUBLIC_URL=https://document.luxit.app \
CONTRACT_ID=735551c2-ec6c-41e6-976d-1eef4e13bfa5 \
scripts/documenso/live_documenso_vps_audit.sh
```

The helper writes a timestamped `documenso-live-audit-*` directory containing
redacted Docker container lists, runtime URL/mail/webhook env values, compose/env
file search results, recent logs filtered around the affected contract and
signing/webhook terms, mounted config/template searches for the broken path, and
a Documenso public URL reachability check. It exits with code `2` when it is run
outside the VPS or any host without Docker.

## GitHub Actions remote audit workflow

Use the **Documenso VPS Audit** workflow (`.github/workflows/documenso-vps-audit.yml`) when direct shell access is unavailable. Trigger it with `workflow_dispatch`, keep the default contract id unless investigating a different contract, and download the `documenso-live-audit-*` artifact when it completes. The workflow copies only the Documenso audit scripts to the VPS, runs the live audit over SSH, captures stdout/stderr, redacts common secret assignments, and uploads the audit bundle for review.

