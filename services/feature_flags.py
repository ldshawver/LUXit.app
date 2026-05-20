"""
PostHog feature flag helpers for LUXit.

Usage:
    from services.feature_flags import is_sms_enabled, sms_feature_required

    # In a view function:
    if not is_sms_enabled(current_user):
        abort(403)

    # As a decorator (requires login_required applied first):
    @twilio_bp.route("/inbox")
    @login_required
    @sms_feature_required
    def inbox(): ...
"""
import os
import time
import logging
import functools
from typing import Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# In-memory cache  {cache_key: (value: bool, expires_at: float)}              #
# --------------------------------------------------------------------------- #
_cache: dict = {}
_CACHE_TTL = 60   # seconds — re-check PostHog at most once per minute per user


FLAG_SMS = "SMS-features"

# Webhook paths that Twilio calls directly — never guarded by feature flags
_TWILIO_WEBHOOK_PATHS = {
    "/twilio/sms/inbound",
    "/twilio/sms/status",
    "/twilio/voice/inbound",
    "/twilio/voice/no-answer",
    "/twilio/voice/recording",
    "/twilio/voice/status",
}


# --------------------------------------------------------------------------- #
# Core checker                                                                 #
# --------------------------------------------------------------------------- #

def _posthog_client():
    """Return the shared PostHog client, or None."""
    try:
        from services.posthog_client import _get_client
        return _get_client()
    except Exception:
        return None


def _cache_key(flag: str, distinct_id) -> str:
    return f"{flag}::{distinct_id}"


def _cached_get(key: str):
    entry = _cache.get(key)
    if entry and time.monotonic() < entry[1]:
        return entry[0]   # True / False
    return None           # miss / expired


def _cache_set(key: str, value: bool):
    _cache[key] = (value, time.monotonic() + _CACHE_TTL)


def check_flag(flag: str, user, company=None) -> bool:
    """
    Return True if `flag` is enabled for this user/company.

    Fail-safe rules:
      - PostHog unavailable → False (disabled) UNLESS user is platform admin.
      - Flag value None/missing → False.
    """
    if user is None:
        return False

    distinct_id = str(user.id)
    ckey = _cache_key(flag, distinct_id)

    cached = _cached_get(ckey)
    if cached is not None:
        return cached

    ph = _posthog_client()
    if ph is None:
        # No PostHog — admins get pass-through; everyone else blocked
        result = bool(getattr(user, "is_admin", False))
        _cache_set(ckey, result)
        return result

    try:
        person_props = {
            "email":        getattr(user, "email", None),
            "role":         "admin" if getattr(user, "is_admin", False) else "member",
        }
        if company:
            person_props.update({
                "company_id":   str(company.id),
                "tenant_id":    str(company.id),
                "company_name": company.name,
                "plan":         getattr(company, "billing_tier", None),
            })

        raw = ph.get_feature_flag(flag, distinct_id, person_properties=person_props)
        result = raw is True or (isinstance(raw, str) and raw.lower() == "true")
    except Exception as exc:
        logger.warning("PostHog flag check failed (%s): %s", flag, exc)
        result = bool(getattr(user, "is_admin", False))

    _cache_set(ckey, result)

    # Track the check event (fire-and-forget)
    try:
        from services.posthog_client import track_event
        track_event(distinct_id, "sms_feature_flag_checked", {
            "flag":       flag,
            "enabled":    result,
            "company_id": str(company.id) if company else None,
            "source":     "backend",
        })
        if result:
            track_event(distinct_id, "sms_feature_enabled", {
                "flag":       flag,
                "company_id": str(company.id) if company else None,
            })
    except Exception:
        pass

    return result


def is_sms_enabled(user, company=None) -> bool:
    """Convenience wrapper for the SMS-features flag."""
    return check_flag(FLAG_SMS, user, company)


# --------------------------------------------------------------------------- #
# Flask decorator                                                              #
# --------------------------------------------------------------------------- #

def sms_feature_required(fn):
    """
    Decorator: blocks the view if SMS-features flag is off.
    Must be applied AFTER @login_required.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from flask import abort, request, jsonify
        from flask_login import current_user

        if not current_user.is_authenticated:
            abort(401)

        company = None
        try:
            company = current_user.get_default_company()
        except Exception:
            pass

        enabled = is_sms_enabled(current_user, company)

        if not enabled:
            _track_blocked(current_user, company)
            # Return JSON for API endpoints, redirect for UI
            if request.is_json or request.path.startswith("/api/"):
                return jsonify(
                    error="SMS features are not enabled for this account.",
                    flag=FLAG_SMS,
                    enabled=False,
                ), 403
            from flask import render_template
            return render_template("sms_blocked.html"), 403

        return fn(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- #
# Before-request hook (applied at blueprint level)                             #
# --------------------------------------------------------------------------- #

def sms_blueprint_guard():
    """
    Call this inside a @blueprint.before_request handler.
    Returns a response to abort with, or None to let the request through.
    """
    from flask import request, jsonify, render_template
    from flask_login import current_user

    # Always allow Twilio webhooks (unauthenticated callbacks from Twilio's servers)
    if request.path in _TWILIO_WEBHOOK_PATHS:
        return None

    if not current_user.is_authenticated:
        return None   # login_required will handle this

    company = None
    try:
        company = current_user.get_default_company()
    except Exception:
        pass

    if is_sms_enabled(current_user, company):
        return None  # allow through

    _track_blocked(current_user, company)

    if request.is_json or request.path.startswith("/api/"):
        return jsonify(
            error="SMS features are not enabled for this account.",
            flag=FLAG_SMS,
            enabled=False,
        ), 403

    return render_template("sms_blocked.html"), 403


def _track_blocked(user, company):
    try:
        from services.posthog_client import track_event
        track_event(str(user.id), "sms_feature_blocked", {
            "flag":       FLAG_SMS,
            "company_id": str(company.id) if company else None,
            "path":       __import__("flask").request.path,
        })
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Cache invalidation (call on login or company change)                         #
# --------------------------------------------------------------------------- #

def invalidate_user_flags(user_id):
    """Purge all cached flag values for a user so they re-check on next request."""
    to_del = [k for k in _cache if k.endswith(f"::{user_id}")]
    for k in to_del:
        _cache.pop(k, None)
