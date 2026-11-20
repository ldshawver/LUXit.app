# Workspace data export, closure and purge

How a customer leaves LUXit, and what the operator does afterwards.

## Customer side (Billing page → "Your data" / "Close this workspace")

| Action | Who | Route | Effect |
|---|---|---|---|
| Export | workspace owner or admin (or platform admin) | `GET /api/account/export` | ZIP: `manifest.json`, `workspace.json`, `members.json`, `tables/<table>.jsonl` for every table that holds the workspace's rows (has `company_id`, or reaches such a table by foreign key). Credential tables (`*secret*`, `*token*`, `*session*`, `twilio_account`, `mobile_device`, `webhook*` …) and credential columns (`*password*`, `*_hash`, `*_sid`, `stripe_*` …) are left out and listed in the manifest. Audit event `account.data_export`. |
| Close | workspace owner (or platform admin) | `POST /api/account/close` `{confirm_name}` | Refused with 409 `subscription_active` while a Stripe subscription still bills (cancel in the portal first; *cancel at period end* counts as cancelled). Marks the company inactive, writes `workspace_closure` (purge date = now + `LUXIT_WORKSPACE_RETENTION_DAYS`, default 30), cancels the phone/SMS license, revokes pending invitations and every LUX Connect session, moves members who belong to another active workspace there. Audit event `account.closed`. |

After closing, members whose default is the closed workspace get `/account/closed`
(pages) or `410 {code: workspace_closed}` (API). Native app sessions were revoked;
new native sign-ins are refused because the company is inactive. A company that
is inactive **without** a `workspace_closure` row behaves exactly as before.

## Operator side

* **Reopen** before the purge date: `/saas` → `POST /saas/accounts/<id>/reopen`
  (platform admin). The phone/SMS license is **not** restored automatically.
* **Purge** after the purge date (take a backup first; staging before prod):

  ```bash
  python scripts/purge_closed_workspaces.py              # dry run: what is due, row counts
  python scripts/purge_closed_workspaces.py --apply      # delete
  ```

  One transaction per workspace. Deletes child tables before parents, clears
  same-table links first, removes memberships, keeps the `company` row as a
  tombstone ("Closed workspace <id>", optional fields cleared). Keeps the
  append-only `security_audit_event` and `marketing_audit_log` (consent evidence)
  and the `workspace_closure` record. User accounts are not deleted (a person
  may belong to other workspaces); delete a person's account through the
  existing user-deletion flow.

## Evidence

* `tests/test_workspace_lifecycle.py` (10): export content/scope/secrets, role
  checks, subscription guard, closure effects, member gate + move, bystander
  tenant untouched, inactive-without-closure unchanged, reopen, purge retention /
  dry run / apply / double purge / reopened never purged.
* `scripts/acceptance/isolated_purge_rehearsal.sh` (real PostgreSQL, foreign keys
  enforced): copy of staging (15 workspaces, ledger 42 → 51), every workspace
  closed and purged in turn → 15/15 ok, 0 leftover rows, 0 changes to other
  workspaces' row counts. The first run found `marketing_audit_log` is
  append-only by trigger; it is now kept.

## Owner decisions

* **D-9 retention** of audit tables after purge (security + consent evidence).
  Recommended default: keep 4 years (TCPA statute of limitations), then a
  separate scheduled delete.
* **Retention window** before purge: default 30 days (`LUXIT_WORKSPACE_RETENTION_DAYS`).
* Purge is operator-run. Recommended: monthly, after the backup drill, until a
  scheduled job is approved.
