"""Tenant-scoped primary-key lookups for route handlers.

A bare ``Model.query.get_or_404(id)`` lets any signed-in user read or change
another workspace's row just by guessing its id. These helpers resolve the
caller's active company the same way the rest of the app does and only return
rows that belong to it. Platform admins are scoped too: they switch workspace
explicitly (set-default-company) instead of reaching across by id.
"""
from flask import abort
from flask_login import current_user


def current_company_id():
    try:
        company = current_user.get_default_company() if hasattr(current_user, "get_default_company") else None
        return company.id if company else getattr(current_user, "default_company_id", None)
    except Exception:
        return getattr(current_user, "default_company_id", None)


def tenant_get(model, obj_id):
    """Return the row only if it belongs to the caller's company, else None."""
    company_id = current_company_id()
    if company_id is None or obj_id is None:
        return None
    return model.query.filter_by(id=obj_id, company_id=company_id).first()


def tenant_get_or_404(model, obj_id):
    obj = tenant_get(model, obj_id)
    if obj is None:
        abort(404)
    return obj
