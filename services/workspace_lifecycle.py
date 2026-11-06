"""Workspace data export, closure and purge (account management).

Export
    ``write_export(company_id, fileobj)`` writes a ZIP: ``manifest.json`` plus
    one JSON-lines file per table that holds this workspace's rows. A table
    belongs to the export when it has a ``company_id`` column, or when it
    reaches such a table through foreign keys (``deal_activity`` → ``deal``).
    Tables and columns that hold credentials, tokens, hashes or sessions are
    left out by name, and the manifest lists what was left out.

Closure
    ``close_workspace`` is the owner's "cancel my account": it refuses while a
    Stripe subscription is still billing, then marks the workspace inactive,
    records a ``workspace_closure`` row with a purge date, cancels the phone/SMS
    license, revokes pending invitations and every LUX Connect session, and
    moves members' default workspace elsewhere. ``closure_blocks_access``
    is what the request gate asks. A platform admin can reopen it before the
    purge.

Purge
    ``purge_workspace`` deletes every exported table's rows for a closed
    workspace whose ``purge_after`` has passed (children first), removes
    memberships, and keeps the ``company`` row as a tombstone with its
    optional text fields cleared, so other tables' references stay valid.
    The append-only audit tables (security trail, marketing/consent audit)
    are kept. Dry run by default.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from extensions import db

DEFAULT_RETENTION_DAYS = 30

# Whole tables never exported (credentials, sessions, internal cursors).
_SENSITIVE_TABLE = re.compile(
    r"(secret|token|credential|oauth|session|password|mobile_device|api_key|twilio_account|"
    r"vault|auth_attempt|verification|billing_event_cursor|security_audit|workspace_closure|"
    r"provider_config|webhook)", re.I)
# Columns never exported, in any table.
_SENSITIVE_COLUMN = re.compile(
    r"(password|passwd|secret|token|api_key|apikey|private_key|credential|_hash$|^hash$|"
    r"signature|otp|cookie|encrypted|(^|_)auth(_|$)|access_key|refresh|webhook_url|_sid$|"
    r"stripe_)", re.I)

# Kept by purge: append-only by database trigger (security trail, marketing /
# consent audit evidence), or the closure record itself. Neither audit table
# has a foreign key to the deleted rows. Retention period = owner decision D-9.
_PURGE_KEEP = frozenset({"company", "security_audit_event", "marketing_audit_log", "workspace_closure"})
# company columns the tombstone keeps.
_TOMBSTONE_KEEP = frozenset({"id", "name", "is_active", "created_at", "updated_at"})

SUBSCRIPTION_STILL_BILLING = ("active", "trialing", "past_due", "unpaid", "incomplete")


class ClosureError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("LUXIT_WORKSPACE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


# ── which tables hold a workspace's data ─────────────────────────────────── #

def _scope_map():
    """{table: None} for tables with company_id, {table: fk} for tables that
    reach one through a foreign key. Computed from the model metadata."""
    tables = db.metadata.tables
    scope = {t: None for t in tables.values() if "company_id" in t.c and t.name != "company"}
    changed = True
    while changed:
        changed = False
        for t in tables.values():
            if t in scope or t.name == "company":
                continue
            for fk in sorted(t.foreign_keys, key=lambda f: f.parent.name):
                parent = fk.column.table
                if parent in scope:
                    scope[t] = fk
                    changed = True
                    break
    return scope


def _condition(table, company_id, scope):
    fk = scope[table]
    if fk is None:
        return table.c.company_id == company_id
    parent = fk.column.table
    return fk.parent.in_(sa.select(fk.column).where(_condition(parent, company_id, scope)))


def _exportable(table):
    return not _SENSITIVE_TABLE.search(table.name)


def _jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    return value


# ── export ───────────────────────────────────────────────────────────────── #

def write_export(company_id: int, fileobj) -> dict:
    """Write the workspace's data as a ZIP to ``fileobj``; return the manifest."""
    from models import Company, User, UserCompanyAccess

    company = db.session.get(Company, company_id)
    if company is None:
        raise ClosureError("not_found", "Workspace not found.", 404)
    scope = _scope_map()
    manifest = {
        "format": "luxit-workspace-export/1",
        "workspace": {"id": company.id, "name": company.name},
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "tables": {},
        "excluded_tables": sorted(t.name for t in scope if not _exportable(t)),
        "excluded_columns": {},
    }
    with zipfile.ZipFile(fileobj, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        company_cols = [c for c in Company.__table__.c if not _SENSITIVE_COLUMN.search(c.name)]
        row = db.session.execute(sa.select(*company_cols).where(Company.__table__.c.id == company_id)).mappings().first()
        zf.writestr("workspace.json", json.dumps({k: _jsonable(v) for k, v in dict(row).items()}, indent=2))

        members = (db.session.query(User.id, User.username, User.email, User.first_name, User.last_name,
                                    UserCompanyAccess.role, UserCompanyAccess.is_active)
                   .join(UserCompanyAccess, UserCompanyAccess.user_id == User.id)
                   .filter(UserCompanyAccess.company_id == company_id).all())
        zf.writestr("members.json", json.dumps([
            {"id": m.id, "username": m.username, "email": m.email, "first_name": m.first_name,
             "last_name": m.last_name, "role": m.role, "active": bool(m.is_active)} for m in members], indent=2))

        for table in sorted(scope, key=lambda t: t.name):
            if not _exportable(table):
                continue
            cols = [c for c in table.c if not _SENSITIVE_COLUMN.search(c.name)]
            dropped = sorted(c.name for c in table.c if _SENSITIVE_COLUMN.search(c.name))
            if dropped:
                manifest["excluded_columns"][table.name] = dropped
            count = 0
            buf = io.StringIO()
            try:
                with db.session.begin_nested():
                    result = db.session.execute(sa.select(*cols).where(_condition(table, company_id, scope)))
                    for rec in result.mappings():
                        buf.write(json.dumps({k: _jsonable(v) for k, v in rec.items()}, default=str))
                        buf.write("\n")
                        count += 1
            except sa.exc.SQLAlchemyError:
                # Table not created on this instance (pending migration): note it, keep going.
                manifest["tables"][table.name] = {"rows": 0, "error": "unavailable"}
                continue
            if count:
                zf.writestr(f"tables/{table.name}.jsonl", buf.getvalue())
            manifest["tables"][table.name] = {"rows": count}
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


# ── closure ──────────────────────────────────────────────────────────────── #

def _open_closure(company_id):
    from models import WorkspaceClosure
    try:
        with db.session.begin_nested():
            row = db.session.get(WorkspaceClosure, company_id)
    except sa.exc.SQLAlchemyError:
        return None
    if row is None or row.reopened_at is not None:
        return None
    return row


def closure_blocks_access(company) -> bool:
    """True when members must not use this workspace (closed and not reopened).
    Only workspaces closed through ``close_workspace`` are blocked; an
    inactive company without a closure row keeps today's behavior."""
    if company is None or bool(getattr(company, "is_active", True)):
        return False
    return _open_closure(company.id) is not None


def can_close(user, company_id) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_admin", False):
        return True
    access = user.get_company_access(company_id)
    return access is not None and access.role == "owner"


def _subscription_still_billing(company) -> bool:
    if not company.stripe_subscription_id:
        return False
    status = (company.stripe_subscription_status or "").lower()
    return status in SUBSCRIPTION_STILL_BILLING and not bool(company.cancel_at_period_end)


def _move_member_defaults(company_id):
    """Point members at another active workspace they belong to. Members with
    none keep the closed one as default, so the gate shows them the closed page."""
    from models import Company, User, UserCompanyAccess
    moved = 0
    for user in User.query.filter(User.default_company_id == company_id).all():
        alt = (db.session.query(UserCompanyAccess.company_id)
               .join(Company, Company.id == UserCompanyAccess.company_id)
               .filter(UserCompanyAccess.user_id == user.id, UserCompanyAccess.company_id != company_id,
                       UserCompanyAccess.is_active.is_(True), Company.is_active.is_(True))
               .order_by(UserCompanyAccess.company_id).first())
        if alt:
            user.default_company_id = alt[0]
            moved += 1
    return moved


def close_workspace(company, actor, *, confirm_name: str, reason: str | None = None, now=None):
    from models import MobileDevice, TeamInvitation, WorkspaceClosure
    from services.license_service import PHONE_PWA_FEATURE, cancel_license
    from services.mobile_auth import revoke_device_sessions

    now = now or datetime.utcnow()
    if (confirm_name or "").strip() != (company.name or "").strip():
        raise ClosureError("confirm_mismatch", "Type the workspace name exactly to confirm.", 400)
    if _subscription_still_billing(company):
        raise ClosureError("subscription_active",
                           "Cancel the subscription in the billing portal first, then close the workspace.")
    existing = db.session.get(WorkspaceClosure, company.id)
    if existing is not None and existing.reopened_at is None:
        raise ClosureError("already_closed", "This workspace is already closed.")
    if existing is not None and existing.purged_at is not None:
        raise ClosureError("purged", "This workspace's data has been deleted.")

    purge_after = now + timedelta(days=retention_days())
    if existing is None:
        existing = WorkspaceClosure(company_id=company.id)
        db.session.add(existing)
    existing.closed_at = now
    existing.closed_by_user_id = getattr(actor, "id", None)
    existing.reason = (reason or "")[:120] or None
    existing.purge_after = purge_after
    existing.reopened_at = None

    company.is_active = False
    TeamInvitation.query.filter(TeamInvitation.company_id == company.id,
                                TeamInvitation.accepted_at.is_(None),
                                TeamInvitation.revoked_at.is_(None)).update(
        {TeamInvitation.revoked_at: now}, synchronize_session=False)
    sessions = 0
    for device in MobileDevice.query.filter_by(company_id=company.id).all():
        sessions += revoke_device_sessions(device, "workspace_closed")
    moved = _move_member_defaults(company.id)
    db.session.commit()
    cancel_license(company.id, PHONE_PWA_FEATURE, actor_user_id=getattr(actor, "id", None), actor_role="owner")
    return {"closed_at": now.isoformat(), "purge_after": purge_after.isoformat(),
            "mobile_sessions_revoked": sessions, "members_moved": moved}


def reopen_workspace(company, actor, now=None):
    from models import WorkspaceClosure
    now = now or datetime.utcnow()
    row = db.session.get(WorkspaceClosure, company.id)
    if row is None or row.reopened_at is not None:
        raise ClosureError("not_closed", "This workspace is not closed.")
    if row.purged_at is not None:
        raise ClosureError("purged", "This workspace's data has been deleted.")
    row.reopened_at = now
    company.is_active = True
    db.session.commit()
    return {"reopened_at": now.isoformat()}


# ── purge ────────────────────────────────────────────────────────────────── #

def due_for_purge(now=None):
    from models import WorkspaceClosure
    now = now or datetime.utcnow()
    return (WorkspaceClosure.query
            .filter(WorkspaceClosure.reopened_at.is_(None), WorkspaceClosure.purged_at.is_(None),
                    WorkspaceClosure.purge_after <= now)
            .order_by(WorkspaceClosure.company_id).all())


def purge_workspace(company_id: int, *, apply: bool = False, now=None) -> dict:
    """Count (dry run) or delete a closed workspace's data. Refuses unless the
    workspace is closed, not reopened, not already purged, and past
    ``purge_after``. Deletes children before parents in one transaction."""
    from models import Company, User, WorkspaceClosure

    now = now or datetime.utcnow()
    closure = db.session.get(WorkspaceClosure, company_id)
    if closure is None or closure.reopened_at is not None:
        raise ClosureError("not_closed", f"Workspace {company_id} is not closed.")
    if closure.purged_at is not None:
        raise ClosureError("purged", f"Workspace {company_id} was already purged.")
    if closure.purge_after > now:
        raise ClosureError("retention", f"Workspace {company_id} is retained until {closure.purge_after.isoformat()}.")
    company = db.session.get(Company, company_id)
    if company is None or company.is_active:
        raise ClosureError("not_closed", f"Workspace {company_id} is active.")

    scope = _scope_map()
    order = [t for t in reversed(db.metadata.sorted_tables) if t in scope and t.name not in _PURGE_KEEP]
    counts = {}
    for table in order:
        cond = _condition(table, company_id, scope)
        n = db.session.execute(sa.select(sa.func.count()).select_from(table).where(cond)).scalar() or 0
        if n:
            counts[table.name] = n
    if not apply:
        db.session.rollback()
        return {"company_id": company_id, "applied": False, "rows": counts}

    try:
        # Rows that point at another row of the same workspace (contact merges,
        # threaded replies) would block a parent delete: clear those links first.
        for table in order:
            for fk in table.foreign_keys:
                if fk.column.table is table and fk.parent.nullable:
                    db.session.execute(sa.update(table).where(_condition(table, company_id, scope))
                                       .values({fk.parent.name: None}))
        for table in order:
            if table.name in counts:
                db.session.execute(sa.delete(table).where(_condition(table, company_id, scope)))
        db.session.execute(sa.update(User.__table__).where(User.__table__.c.default_company_id == company_id)
                           .values(default_company_id=None))
        values = {}
        for col in Company.__table__.c:
            if col.name in _TOMBSTONE_KEEP or col.primary_key or not col.nullable:
                continue
            values[col.name] = None
        values["name"] = f"Closed workspace {company_id}"
        db.session.execute(sa.update(Company.__table__).where(Company.__table__.c.id == company_id).values(**values))
        closure.purged_at = now
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return {"company_id": company_id, "applied": True, "rows": counts}
