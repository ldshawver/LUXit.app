# Codex Implementation Instructions: Phone-Line Webhooks, Contact Segmentation, and SMS Campaign Operations

## Objective

Build a production-grade contact, audience segmentation, phone-number configuration, and SMS campaign system that makes it impossible to confuse one phone line's webhooks, compliance settings, or sender configuration with another. The result must let users fully manage scheduled SMS/MMS campaigns, import and segment contacts from CRM and spreadsheet sources, test campaigns before scheduling, throttle sends safely, and surface campaigns consistently in analytics, reports, AI workflows, and the marketing calendar.

## Non-negotiable outcomes

1. Every phone number must have its own visible configuration record, including inbound SMS webhook, delivery-status webhook, voice webhook when applicable, compliance profile, sender limits, default opt-out language, and ownership/tenant metadata.
2. Tenant-level or API-level Twilio defaults may remain as fallback values, but the UI must clearly show when a phone line inherits a fallback versus when it has line-specific settings.
3. A scheduled SMS campaign must be fully editable until it enters an actively sending or completed terminal state.
4. Scheduled campaigns must support delete/cancel, copy/duplicate, test send, audience preview, schedule changes, content changes, sender-number changes, attachment changes, and compliance validation.
5. Audience selection must support CRM contacts, LUX audience segments, explicitly selected contacts, uploaded CSV/XLSX rows, and combinations of these sources with deduplication and suppression.
6. Imports must use a single documented contact template with deterministic column mapping for CRM fields, consent fields, segment fields, keyword fields, notes, and suppression fields.
7. SMS/MMS sending must run in batches with configurable send-rate controls, quiet hours, opt-out enforcement, provider error handling, and durable per-recipient status tracking.
8. Campaigns and their outcomes must be visible in analytics, reporting, marketing calendar views, and AI assistant context without leaking secrets or cross-tenant data.

## Phase 1: Discovery and safety audit

Before changing behavior, map the current implementation and write down where each concern lives.

- Locate current Twilio configuration code and confirm whether it reads from environment variables, tenant/company records, phone-number records, or a mixture of these.
- Locate the inbound SMS webhook routes, status callback routes, voice webhook routes, and any forwarding scripts.
- Locate SMS campaign models, routes, templates, scheduler jobs, recipient tables, analytics queries, and contact/segment models.
- Identify all places where a campaign can be created, sent, scheduled, tested, copied, deleted, or shown in navigation.
- Identify every suppression or consent field already present and determine whether any are ambiguous or duplicated.
- Confirm tenant isolation on all contact, campaign, phone-number, segment, upload, and analytics queries.
- Add tests for existing behavior before refactoring if coverage is missing around campaign sending or webhook routing.

## Phase 2: Phone-line configuration and webhook management

### Data model requirements

Create or extend a `PhoneNumber`/`TwilioPhoneNumber` style model so each line has first-class settings:

- Tenant/company ID.
- Provider name, currently `twilio`, while leaving room for future providers.
- E.164 phone number.
- Friendly display name.
- Capabilities: SMS, MMS, voice, WhatsApp if applicable.
- Inbound SMS webhook URL.
- Delivery-status callback URL.
- Voice webhook URL.
- Emergency/fallback forwarding number if supported.
- Messaging service SID if this line belongs to a Twilio Messaging Service.
- Provider phone-number SID.
- Active/inactive state.
- Default sender identity for campaigns.
- Compliance profile or campaign registration reference where applicable.
- Default opt-out/help language.
- Per-line send-rate limit.
- Per-line daily cap.
- Per-line quiet-hours policy and timezone.
- Last webhook verification time, last provider sync time, and last provider error.
- Audit fields for who changed the configuration and when.

### UI requirements

Add a clear settings area such as **Settings > Communications > Phone Lines** or **Admin > Phone Lines**.

The page must:

- List each phone line with tenant, display name, number, status, capabilities, compliance state, and active campaign usage.
- Show badges for `Line-specific`, `Inherited from tenant default`, and `Missing/invalid` settings.
- Provide detail/edit screens for each phone line.
- Show webhook URLs in copyable fields.
- Provide a provider sync/check button that compares local values to Twilio values without exposing secrets.
- Provide a webhook verification checklist and last verification timestamp.
- Warn before changing a webhook or sender setting used by active or scheduled campaigns.
- Prevent users from editing phone lines outside their tenant.
- Provide role-based permissions so only authorized admins/managers can modify line settings.

### Twilio/API behavior

- Prefer line-specific settings when present.
- Fall back to tenant-level Twilio account settings only when explicitly allowed and visible in the UI.
- Fall back to platform environment variables only for legacy compatibility and clearly mark this as a legacy fallback.
- Never log or render auth tokens, API keys, client secrets, or full provider credentials.
- Store provider identifiers, message SIDs, error codes, callback payload metadata, and verification state only.
- Add a validation routine that can tell the user exactly which setting will be used for a given phone line and campaign.

## Phase 3: Unified contact model and import template

### Contact fields

Ensure the contact model and import pipeline can represent a complete marketing and CRM matrix:

- Required identity fields: first name, last name, phone, email.
- Optional identity fields: company, title, birthday, address, city, state, postal code, country, timezone.
- CRM fields: lifecycle stage, lead source, owner, tags, status, custom field JSON, account/company name, opportunity stage, estimated value.
- Marketing fields: keyword, segment names, audience labels, campaign source, UTM source, UTM medium, UTM campaign.
- Consent fields: SMS marketing opt-in, SMS opt-in timestamp, SMS opt-in source, email marketing opt-in, email opt-in timestamp, email opt-in source.
- Suppression fields: do not market, do not SMS, do not email, SMS opted out, email unsubscribed, hard bounced, complaint/spam complaint.
- Preference fields: preferred channel, preferred language, quiet-hours preference, frequency cap preference.
- Notes fields: internal notes, public notes, import batch notes.
- External IDs: CRM ID, ecommerce customer ID, Twilio identity if applicable, source system ID.

### Single import template

Provide one canonical template for all contact uploads. Support both CSV and XLSX downloads.

Template columns should include at least:

```text
first_name,last_name,phone,email,company,title,birthday,address_line_1,address_line_2,city,state,postal_code,country,timezone,crm_lifecycle_stage,crm_lead_source,crm_owner,crm_status,opportunity_stage,opportunity_value,tags,segment_names,keyword,audience_labels,preferred_channel,preferred_language,sms_marketing_opt_in,sms_opt_in_at,sms_opt_in_source,email_marketing_opt_in,email_opt_in_at,email_opt_in_source,do_not_market,do_not_sms,do_not_email,sms_opted_out,email_unsubscribed,hard_bounced,spam_complaint,utm_source,utm_medium,utm_campaign,external_crm_id,ecommerce_customer_id,source_system_id,notes,custom_fields_json
```

### Import UX requirements

- Provide a downloadable CSV template and XLSX template from contact import and campaign audience screens.
- Show a preview step before committing imports.
- Auto-map exact template headers.
- Allow manual mapping for non-template uploads, but warn that the canonical template is recommended.
- Validate phone numbers into E.164 format.
- Validate email format.
- Detect duplicate contacts by tenant plus normalized phone/email/external ID.
- Let users choose update strategy: skip duplicates, update empty fields only, overwrite selected fields, or create a new import list without updating master contacts.
- Record import batch ID, source filename, uploader, row count, accepted rows, rejected rows, and rejection reasons.
- Generate a downloadable error report for bad rows.
- Apply suppression before campaign eligibility.
- Never let an import undo an opt-out unless the import has an explicit lawful opt-in source and timestamp and the business rules permit re-opt-in.

## Phase 4: Audience segmentation and access navigation

### Segment capabilities

Segments should support:

- Static membership.
- Dynamic rule-based membership.
- Imported-list membership.
- Keyword-based membership.
- CRM-field membership.
- Ecommerce behavior membership.
- Campaign engagement membership.
- Explicit inclusions and exclusions.
- Permanent exclusion from a segment.
- Preview counts and sample rows before saving.
- Copy/duplicate segment.
- Segment versioning or audit log for rule changes.

### Campaign audience builder

The SMS campaign audience builder must support:

- Select one or more segments.
- Select individual CRM contacts.
- Upload a CSV/XLSX audience file.
- Paste phone numbers manually for small test groups if allowed by policy.
- Include or exclude specific contacts.
- Include or exclude segment(s).
- Deduplicate by normalized phone number and contact ID.
- Preview eligible, suppressed, invalid, duplicate, and missing-consent counts.
- Show exact skip reasons before scheduling.
- Save the resolved audience snapshot for scheduled campaigns so the user knows whether it is a dynamic send-at-time audience or a fixed locked audience.

### Navigation requirements

Contacts and audiences must be easy to access from:

- CRM navigation.
- Sales navigation.
- Campaign creation/editing.
- Audience/segments navigation.
- Analytics reports.
- AI assistant context/actions.
- Contact detail pages.
- Marketing calendar campaign detail pages.

## Phase 5: Fully editable scheduled SMS/MMS campaigns

### Campaign states

Use explicit campaign states such as:

- Draft.
- Scheduled.
- Queued.
- Sending.
- Paused.
- Completed.
- Canceled.
- Failed.
- Archived.

Only allow edits that are safe for the current state:

- Draft: all fields editable.
- Scheduled: all fields editable until queue lock time.
- Queued: allow pause/cancel only unless not yet materialized.
- Sending: allow pause/cancel, but do not silently edit already queued recipients.
- Completed/canceled/failed: allow duplicate/copy and archive, not destructive edit.

### Editable fields

Users must be able to edit before lock time:

- Campaign name.
- Sender phone line.
- Message body.
- URLs.
- Media attachments.
- Audience selection.
- Specific included/excluded contacts.
- Send date.
- Send time.
- Timezone.
- Batch size.
- Send rate.
- Quiet-hours handling.
- Compliance footer/STOP wording where policy allows.
- Tracking parameters.
- Internal notes.

### Copy, delete, cancel, and archive

- Duplicate must create a draft copy with message, media references, audience rules, sender line, rate settings, and notes, but no recipient delivery statuses.
- Delete should be limited to drafts where safe.
- Scheduled campaigns should use cancel rather than hard delete if recipient snapshots or audit records exist.
- Archive should hide old completed/canceled campaigns from default lists while preserving auditability.
- All destructive actions require confirmation and audit logging.

### Test SMS/MMS

Before scheduling, users must be able to send a test message:

- Send to one or more test numbers or contacts.
- Use the selected sender phone line.
- Render personalization tokens.
- Include URLs and media attachments.
- Mark messages as test in logs and analytics.
- Show provider SID, status, and error if the test fails.
- Prevent test sends to opted-out numbers unless the user has explicit admin override and the message is non-marketing; default should be no override.

## Phase 6: Message content, URLs, and MMS media

- Support URLs in SMS text without corrupting tracking parameters.
- Provide optional URL tracking and UTM builder.
- Validate message segment count and display estimated SMS segment cost.
- Support MMS image attachments when the selected phone line/provider supports MMS.
- Validate media MIME type, file size, dimensions, and public accessibility for Twilio.
- Do not imply that MMS image pixels can be natively hyperlinked inside SMS/MMS clients. Instead, support a clickable URL in the message body that is associated with the image/card in the composer.
- Provide preview modes for SMS-only and MMS-capable recipients.
- Detect prohibited content patterns where possible and show compliance warnings.

## Phase 7: Batch sending, compliance, and deliverability

### Rate controls

Campaign scheduling must include:

- Batch size.
- Messages per minute or per second.
- Per-phone-line cap.
- Per-tenant cap.
- Daily cap.
- Quiet-hours enforcement.
- Timezone-aware send windows.
- Automatic pause on excessive provider errors.
- Retry policy for transient errors.
- No retry for permanent failures, opt-outs, invalid numbers, or blocked numbers.

### Compliance controls

- Enforce STOP/HELP handling on inbound messages.
- Enforce global `do_not_market` first.
- Enforce channel-specific SMS and email suppression fields.
- Enforce missing-consent exclusion for marketing SMS.
- Store opt-in source and timestamp.
- Store opt-out source and timestamp.
- Keep an immutable compliance event log.
- Include sender identification and opt-out language according to business policy and applicable Twilio requirements.
- Provide warnings around SHAFT-like categories and high-risk content.
- Validate that selected sender line is active, compliant, and allowed for campaign use.

## Phase 8: Analytics, reporting, calendar, and AI visibility

Expose scheduled and sent SMS campaigns in:

- Marketing calendar by scheduled date/time and campaign state.
- Analytics dashboards with sent, delivered, failed, undelivered, queued, skipped, opt-out, reply, click, and conversion metrics where available.
- Reports with exportable CSV.
- AI assistant context so the assistant can answer questions such as upcoming campaigns, failed sends, suppressed audience counts, and recommended send-time adjustments.
- Contact timeline showing campaign membership, delivery status, replies, clicks, and opt-outs.
- Segment analytics showing campaign performance by audience/segment.

All analytics must be tenant-scoped and must never expose provider secrets.

## Phase 9: Permissions, auditing, and guardrails

- Define permissions for viewing contacts, importing contacts, editing contacts, creating segments, editing campaigns, scheduling campaigns, sending tests, changing phone-line settings, and viewing compliance logs.
- Add audit logs for contact imports, consent changes, segment changes, campaign edits, schedule changes, sender line changes, test sends, campaign cancellation, phone-line webhook changes, and provider sync attempts.
- Add confirmation flows for changes that can impact compliance or live scheduled campaigns.
- Add validation to prevent cross-tenant access by route tampering, direct object IDs, or imported IDs.
- Mask phone numbers and emails where role permissions require limited access.

## Phase 10: Recommended database additions

Use migrations with idempotent guards where possible. Consider these tables if not already present:

- `phone_number_settings` for per-line provider/webhook/compliance/send-rate configuration.
- `phone_number_audit_log` for configuration changes and provider sync attempts.
- `contact_import_batch` for upload metadata.
- `contact_import_row_error` for rejected-row diagnostics.
- `contact_consent_event` for opt-in and opt-out history.
- `sms_campaign_version` for scheduled campaign edit history.
- `sms_campaign_audience_snapshot` for locked recipient/audience decisions.
- `sms_campaign_recipient` for per-recipient status, skip reason, provider SID, and provider errors.
- `sms_campaign_media` for MMS attachments.
- `sms_campaign_test_send` for test sends.
- `campaign_calendar_event` if the calendar is not already derived from campaign tables.

## Phase 11: Implementation sequence for Codex

1. Add or update tests that document current behavior around phone-line config, contact suppression, campaign scheduling, and recipient status.
2. Implement/extend data models and migrations.
3. Add backend services for phone-line config resolution and validation.
4. Add contact import template generation, parsing, preview, validation, and batch persistence.
5. Add/extend contact and segment APIs to support unified audience selection.
6. Refactor SMS campaign creation/editing to use a campaign state machine and versioned scheduled edits.
7. Add campaign duplicate, cancel, archive, and safe delete flows.
8. Add test-send flow using selected sender line and selected content/media.
9. Add batch scheduler/rate limiter with durable recipient status updates.
10. Add analytics/calendar/reporting/AI data hooks.
11. Add UI navigation improvements for CRM, contacts, segments, campaigns, phone lines, and reports.
12. Add full regression tests and browser QA evidence.
13. Update operational docs and deployment notes.

## Rigorous test plan

### Unit tests

- Phone-line config resolution chooses line-specific settings before tenant fallback and platform fallback.
- Missing webhook settings produce actionable validation errors.
- Provider secrets are masked in serializers, logs, and templates.
- Contact import accepts the canonical CSV header.
- Contact import accepts the canonical XLSX header.
- Contact import rejects invalid phone and email formats with row-level reasons.
- Duplicate contact detection works by tenant, phone, email, and external ID.
- Import update strategies behave correctly.
- `do_not_market` overrides all marketing eligibility.
- `do_not_sms` and `sms_opted_out` exclude SMS recipients.
- Lawful opt-in timestamps and sources are persisted.
- Segment rules include/exclude contacts correctly.
- Dynamic segment preview counts match saved results.
- Campaign state transitions allow only permitted edits/actions.
- Campaign duplicate copies configuration but not delivery history.
- Campaign audience builder deduplicates contacts and reports skip reasons.
- Batch send logic respects rate limits and quiet hours.
- Provider transient failures retry according to policy.
- Permanent failures do not retry.
- Test sends are marked as tests and excluded from production send counts.

### Integration tests

- Create contacts through CRM and use them in an SMS campaign.
- Upload CSV contacts, preview, commit, and select uploaded contacts in a campaign.
- Upload XLSX contacts, preview, commit, and select uploaded contacts in a campaign.
- Create segment, add contacts, exclude one contact, and verify campaign audience eligibility.
- Schedule campaign, edit send date/time, edit message, edit audience, and verify recipient snapshot behavior.
- Duplicate a scheduled campaign and verify the copy is a draft.
- Cancel a scheduled campaign and verify no sends occur.
- Send a test MMS with image and URL through mocked Twilio.
- Process inbound STOP, HELP, START if supported, and verify consent events.
- Process Twilio status callbacks for delivered, failed, and undelivered messages.
- Verify analytics, reports, calendar, AI context, and contact timeline reflect campaign status.
- Verify users cannot access another tenant's contacts, phone lines, campaigns, imports, or analytics.

### Browser/end-to-end tests

- Navigate from CRM to contacts, segments, campaign builder, and phone-line settings.
- Download CSV and XLSX import templates.
- Upload a valid template and complete preview/commit.
- Upload an invalid file and download the error report.
- Create and schedule an SMS campaign using a segment.
- Edit a scheduled campaign's send date/time.
- Edit a scheduled campaign's audience and content.
- Send a test SMS.
- Duplicate a campaign.
- Cancel a campaign.
- Confirm the campaign appears on the marketing calendar.
- Confirm analytics/reports pages show scheduled and completed campaign metrics.
- Confirm phone-line settings display inherited versus line-specific values clearly.

### Security and compliance tests

- Assert all routes enforce authentication and role permissions.
- Assert all queries are tenant-scoped.
- Assert users cannot edit phone lines or campaigns by guessing IDs from another tenant.
- Assert logs and rendered pages do not include Twilio auth tokens or API secrets.
- Assert import files are scanned/validated for safe extensions and size limits.
- Assert opt-out cannot be overwritten by an ordinary import.
- Assert campaign sends skip suppressed contacts and record skip reasons.
- Assert quiet-hours and daily caps are enforced.
- Assert status callback signatures are validated where applicable.

### Load and reliability tests

- Import at least 10,000 contacts from CSV with deterministic performance and row-level errors.
- Build an audience of at least 10,000 recipients with deduplication and suppression.
- Run a campaign send simulation with provider mocks and configured rate limits.
- Restart the scheduler during a campaign and verify it resumes without duplicate sends.
- Simulate Twilio throttling and verify the campaign pauses or backs off safely.
- Simulate database transaction failure and verify no partial duplicate recipient sends.

## Definition of done

- Phone-line webhook and compliance settings are visible, per-line, auditable, and tenant-safe.
- API/default fallback settings are clearly marked and cannot silently override a line-specific configuration.
- Contacts, segments, imports, CRM, sales, campaigns, analytics, reports, calendar, and AI all use the same tenant-scoped audience model.
- Scheduled SMS/MMS campaigns can be edited, duplicated, tested, canceled, archived, and safely sent in batches.
- Campaign sends respect opt-in, opt-out, do-not-market, quiet hours, send-rate, and provider constraints.
- The canonical CSV/XLSX contact import template is downloadable and fully documented.
- Analytics and reports show campaign lifecycle, audience eligibility, delivery outcomes, and compliance events.
- Automated tests cover unit, integration, browser, security, compliance, load, scheduler-resume, and provider-error scenarios.
- Deployment notes explain migrations, Twilio configuration, webhook setup, rollback, and staging verification.

## Required audit action workflow before PR completion

Codex must treat SMS, CRM audience, PWA inbox, phone-line, and campaign work as an executable audit-and-fix assignment, not as a read-only inspection. A PR is not complete until every audited area has either passed, been fixed and retested, or been explicitly documented as out of scope with a reason and risk owner.

### 1. AUDIT

Run checks that exercise all critical areas touched by the SMS/phone-line system:

- Route health: `/sms/create`, `/app/sms-campaigns`, `/settings/phone-lines`, `/admin/phone-lines`, `/admin/communications`, `/twilio/comms`, `/app/inbox`, and `/api/inbox/conversations?filter=all`.
- Schema compatibility: `twilio_phone_number`, `twilio_account`, `sms_campaign`, `sms_recipient`, `sms_template`, `twilio_conversation`, `twilio_message`, and calendar/marketing event tables.
- Permission safety: admin/owner access, staff/mobile-inbox access, non-admin sender denial for unassigned lines, assigned/permitted sender access, and cross-company leakage prevention.
- Campaign lifecycle: create, edit scheduled content/media/sender/date/time/audience, duplicate, cancel, delete unsent, archive, test send, batch send, resume pending recipients, and duplicate-send prevention.
- Contact import/audience: template download, CSV preview, XLSX preview, header mapping, duplicate detection, text opt-in, email opt-in, do-not-market, SMS opt-out, email opt-out, tags, notes, and company scoping.
- Calendar, analytics, AI context: scheduled SMS visibility, reschedule/cancel/archive status, SMS metrics, AI scheduled/recent context, and AI suggestions/performance summaries.
- PWA history protection: permitted-number conversations remain visible, read conversations remain visible in `filter=all`, browser/PWA reopen does not clear conversations, and shared-number history is not hidden by user-only filters.

### 2. FINDINGS

For every failed audit check, document a finding before implementing the fix. Each finding must include:

- Failing area.
- Exact route, function, table, template, service, or test.
- Error message, missing behavior, unsafe behavior, or observed mismatch.
- Root cause.
- Affected files.
- Risk level: `critical`, `high`, `medium`, or `low`.
- Planned fix and expected verification command.

Use this finding format in the PR body or an audit evidence document:

```markdown
### Finding: <short name>
- Area:
- Route/function/table:
- Error or missing behavior:
- Root cause:
- Affected files:
- Risk level:
- Fix:
- Retest command:
- Result:
```

### 3. FIX

Implement code, migration, template, and test fixes for every confirmed finding. Do not leave known issues in these categories unless they are explicitly out of scope with a reason and owner:

- Route 500s.
- `UndefinedColumn`, `ProgrammingError`, or `BuildError` failures.
- Broken navigation links.
- Unsafe sender fallback to global/API Twilio settings.
- Cross-company contact, campaign, phone-line, conversation, or analytics leakage.
- Staff/mobile-inbox PWA access regressions.
- Missing opt-out/do-not-market enforcement.
- Duplicate sends or unsafe scheduler resume behavior.
- Twilio provider errors that surface as unhandled 500s instead of friendly user-facing messages.

### 4. RETEST

After each fix, rerun the narrowest focused test or command that failed. After all fixes are complete, run the full SMS/phone-line regression suite.

Minimum local retest commands:

```bash
python -m compileall models.py routes.py services/sms_service.py services/phone_line_service.py services/sms_campaign_context_service.py twilio_sms.py
pytest tests/test_sms_crm_phone_line_completion.py tests/test_sms_campaign_after_hours.py -q
pytest tests/test_pwa_phone_system.py::test_staff_mobile_inbox_user_can_log_in_and_open_inbox tests/test_pwa_phone_system.py::test_inbox_conversations_all_includes_read_and_unread_non_archived -q
rg "Company\.query\.first" twilio_sms.py services/sms_service.py services/phone_line_service.py routes.py || true
```

### 5. VERIFY

Include the verification commands and expected results in the PR body and deployment notes:

| Check | Command | Expected result |
| --- | --- | --- |
| Create SMS campaign | `curl -I https://<host>/sms/create` | Authorized user receives 200; no 500 or `BuildError`. |
| SMS campaign dashboard | `curl -I https://<host>/app/sms-campaigns` | Authorized user receives 200. |
| Phone-line settings | `curl -I https://<host>/settings/phone-lines` | Authorized tenant admin receives 200 and sees per-number webhook fields. |
| Admin phone lines | `curl -I https://<host>/admin/phone-lines` | Authorized admin receives 200. |
| Admin communications | `curl -I https://<host>/admin/communications` | Authorized admin receives 200. |
| PWA inbox | `curl -I https://<host>/app/inbox` | Mobile-inbox permitted user receives 200 after login. |
| PWA history API | `curl -s https://<host>/api/inbox/conversations?filter=all` | Returns JSON with permitted-number conversations, including read conversations. |
| Live error log | `journalctl -u luxit --since "30 minutes ago" --no-pager | egrep -i "UndefinedColumn|ProgrammingError|BuildError|permission|Traceback" || true` | No new errors for SMS, phone-line, contact import, or PWA inbox routes. |

### 6. REPORT

The final PR summary must include:

- Audit checks performed.
- Findings discovered.
- Fixes completed for each finding.
- Tests added or updated.
- Tests passed, including exact commands.
- Unresolved risks, if any, with owner and reason.
- Live deployment verification steps.
- Rollback notes for additive migrations and any code rollback path.

Definition of done: Codex must show that each audit finding was either fixed, covered by tests, or explicitly documented as out of scope with a reason. Do not accept a documentation-only or partial audit response for implementation PRs.
