# LUXit Phone ↔ LUXit.app CRM contract (v1)

For the LUX Connect / LUXit Phone client (native and PWA). Session-authenticated JSON, scoped to the
signed-in user's current workspace. Phone numbers are E.164 and **must be URL-encoded** in query strings
(`+` → `%2B`). All responses carry `success`.

| Call | When | Request | Response (abridged) |
|---|---|---|---|
| Caller context | incoming call screen, before answer | `GET /api/comms/caller-context?from=<caller>&to=<business line>` | `{identity:{id,name}\|null, contact:{id,name,lifecycle_stage}\|null, briefing:{summary, consent:{do_not_contact,sms_opted_out,…}, recommendations[≤3], open_opportunities[≤3], open_tasks[≤3]}\|null}` — unknown callers get `contact:null`; never creates a contact |
| Timeline | contact screen | `GET /api/contacts/<id>/timeline?limit=&before=&kinds=message,email,call,voicemail,activity,task,opportunity,deal,touchpoint,source` | `{canonical_contact_id, entries:[{id,kind,channel,direction,at,title,summary,status,source:{table,id},provenance,merged_from}], next_before}` |
| Briefing | contact screen / after a call | `GET /api/contacts/<id>/briefing` | consent, activity, summary, open tasks/opportunities, memories, recommendations (each with `source_refs`) |
| Send text | reply from contact screen | `POST /api/contacts/<id>/send` `{channel:"sms", body, identity_id?}` + header `Idempotency-Key` | 200 sent · 409 `no_conversation` · 422 `do_not_contact`/`sms_opted_out`/`marketing_not_allowed` · 502 provider failure. Retries with the same key never resend |
| Log a call note | after a call | `POST /api/contacts/<id>/activities/add` (form: `activity_type=call, subject, description, outcome, duration_minutes`) | `{success}` |
| Follow-up task | after a call | `POST /api/crm/tasks` `{contact_id, title, due_at, reminder_at?}` | `{task_id}` |
| Today | home tab | `GET /api/crm/dashboard?scope=mine\|all` | `needs_attention[]`, tasks, pipeline summary |

Rules the client must not work around:
- Texts are only sent inside an existing thread on the identity's line (`no_conversation` otherwise); the
  server picks the sender number. There is no client-side sender override.
- Consent is enforced server-side; show the `code` to the user, don't retry policy refusals.
- Viewers get 403 on writes.

Not in v1 (needs separate approval/credentials): outbound-first texting to a number with no thread,
mailbox sync (Graph/Gmail OAuth apps per tenant), tenant email sending (`LUXIT_TENANT_EMAIL_SEND_ENABLED`,
off — today's transport is the platform mailbox), push payloads carrying caller context.
