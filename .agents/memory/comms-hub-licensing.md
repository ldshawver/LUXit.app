---
name: Comms Hub licensing columns
description: Per-user Communications Hub feature toggles added to UserCompanyAccess; access-control method patterns and test gotchas.
---

## New columns on UserCompanyAccess (models.py)

comms_hub_enabled, pwa_access_enabled, calls_enabled, sms_enabled,
voicemail_enabled, ai_comms_enabled, forwarding_enabled,
communications_license, assigned_number, number_type

All added to scripts/migrate_db.py MIGRATIONS list (DEFAULT values provided).

## Access-control methods

`has_mobile_inbox_access()` — owner/admin always True; else pwa_access_enabled OR can_access_mobile_inbox (legacy).
`has_comms_hub_access()` — owner/admin always True; else comms_hub_enabled OR communications_license.

**Why:** Admins always have implicit access so they can't lock themselves out.
Legacy `can_access_mobile_inbox` kept for backward compat (OR logic, not replacement).

## AJAX endpoint

`POST /twilio/comms/settings/user/<user_id>` — admin-only, saves any subset of toggle fields as JSON.
Returns `{success, message, changed}`.

## Test gotchas

- SQLAlchemy ORM instances created with `__new__` lack instrumented attribute state → use a plain Python stand-in class that copies the access-control method logic.
- `monkeypatch.setattr(models.AutoReplyRule, "query", ...)` triggers `flask_sqlalchemy.model.__get__` which needs a live app context → wrap with `app = create_app(); with app.app_context():`.
- `_is_business_hours(company_id)` makes a DB call inside `_apply_auto_reply_rules`; patch it via `monkeypatch.setattr(twilio_sms, "_is_business_hours", lambda cid: True)`.
