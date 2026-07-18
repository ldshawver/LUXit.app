# Patch transfer validation

Validation date: 2026-07-18.

## Verified base

`46101128e01c2ddc316718a581814d802cc403d4` (`Merge pull request #210 from ldshawver/codex/instrument-client-push-registration-lifecycle`).

## Transfer artifacts

Patch artifacts are intentionally not committed to the application branch. They were generated under `/tmp` for transfer:

| Artifact | Size | SHA-256 | Base | Applies independently |
|---|---:|---|---|---|
| `/tmp/luxit-user-archive-fix.patch` | 39733 bytes | `ce6499b4cff678a414b1339204914edaf07b6be51e4043f9f05cadfa5ccd6221` | `46101128e01c2ddc316718a581814d802cc403d4` | Yes |
| `/tmp/luxit-contact-intelligence.patch` | regenerated after removing user-archive references | `da0f38aaa899ad9726f90601e4f1dfe9bc13e30a5e1dd291d57811e9b5c63754` | `46101128e01c2ddc316718a581814d802cc403d4` | Yes |

## User archive-only patch file list

`git apply --stat /tmp/luxit-user-archive-fix.patch` produced 11 files:

- `app.py`
- `auth.py`
- `docs/user_archive_access_audit.md`
- `migrations/20260718_user_archive_restore.sql`
- `models.py`
- `routes.py`
- `services/comms_permissions.py`
- `services/user_lifecycle.py`
- `templates/manage_users.html`
- `tests/test_user_archive_migration_workflow.py`
- `tests/test_user_archive_restore.py`

`git apply --numstat /tmp/luxit-user-archive-fix.patch` produced:

```text
13	2	app.py
11	0	auth.py
16	0	docs/user_archive_access_audit.md
17	0	migrations/20260718_user_archive_restore.sql
26	7	models.py
53	13	routes.py
4	4	services/comms_permissions.py
129	0	services/user_lifecycle.py
37	6	templates/manage_users.html
28	0	tests/test_user_archive_migration_workflow.py
154	0	tests/test_user_archive_restore.py
```

No binary records were present, and the clean user-archive patch did not contain these Contact Intelligence identifiers: `contact_intelligence`, `google_contact`, `contact_dedupe`, `phone_normalization`, `marketing_api`, `twilio_service`, `contact_duplicate_review`, or `20260714_contact_intelligence_crm`.

## Independent patch application

Disposable worktrees were created at the verified base and tested with:

- User archive only: `git apply --check /tmp/luxit-user-archive-fix.patch`, `git apply /tmp/luxit-user-archive-fix.patch`, `git diff --check`.
- Contact Intelligence only: `git apply --check /tmp/luxit-contact-intelligence.patch`, `git apply /tmp/luxit-contact-intelligence.patch`, `git diff --check`.
- User archive then Contact Intelligence: both patches applied and `git diff --check` passed.
- Contact Intelligence then user archive: both patches applied and `git diff --check` passed.

No required order is currently needed; both application orders succeeded in disposable worktrees.

## Staging status

Staging validation has not been performed. Production approval remains blocked until the user archive-only patch is applied and validated on staging.
