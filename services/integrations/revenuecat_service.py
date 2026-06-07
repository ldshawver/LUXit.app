"""
RevenueCat integration service.

Credentials: REVENUECAT_SECRET_KEY (server-side REST API v1 key).
App does not crash if RevenueCat is unavailable — shows a safe fallback.

Entitlement IDs used by LUXit:
  luxit_access, crm_access, sms_messaging, email_campaigns,
  ai_agents, analytics, platform_console, payroll_workforce
"""
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.revenuecat.com/v1"

ENTITLEMENT_IDS = [
    "luxit_access",
    "crm_access",
    "sms_messaging",
    "email_campaigns",
    "ai_agents",
    "analytics",
    "platform_console",
    "payroll_workforce",
]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def health_check() -> dict:
    key = _secret_key()
    if not key:
        return {"status": "missing_config", "detail": "RevenueCat credentials not configured"}
    try:
        r = requests.get(
            f"{_BASE}/subscribers/$RCAnonymousID:test_health",
            headers=_headers(key),
            timeout=10,
        )
        if r.status_code in (200, 404):
            return {"status": "connected"}
        if r.status_code == 401:
            return {"status": "error", "detail": "Invalid API key"}
        return {"status": "error", "detail": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Customer info
# ---------------------------------------------------------------------------

def get_customer_info(app_user_id: str) -> dict:
    """
    Fetch subscriber data for an authenticated LUXit user.
    Never use anonymous IDs — pass the real authenticated user ID.
    Returns safe dict; never raises.
    """
    key = _secret_key()
    if not key:
        return {"ok": False, "reason": "missing_config", "entitlements": {}}
    try:
        r = requests.get(
            f"{_BASE}/subscribers/{app_user_id}",
            headers=_headers(key),
            timeout=15,
        )
        if r.status_code == 404:
            return {"ok": True, "entitlements": {}, "subscriptions": {}}
        r.raise_for_status()
        data = r.json().get("subscriber", {})
        entitlements = {
            k: {
                "expires_date": v.get("expires_date"),
                "product_identifier": v.get("product_identifier"),
            }
            for k, v in data.get("entitlements", {}).items()
        }
        return {
            "ok": True,
            "entitlements": entitlements,
            "subscriptions": data.get("subscriptions", {}),
            "original_app_user_id": data.get("original_app_user_id"),
        }
    except Exception as exc:
        logger.error("RevenueCat get_customer_info(%s): %s", app_user_id, exc)
        return {"ok": False, "reason": "Billing status temporarily unavailable.", "entitlements": {}}


def has_entitlement(app_user_id: str, entitlement_id: str) -> bool:
    """
    Returns True only if user has an active (non-expired) entitlement.
    Returns False safely if RevenueCat is unreachable.
    """
    info = get_customer_info(app_user_id)
    if not info.get("ok"):
        return False
    ent = info.get("entitlements", {}).get(entitlement_id)
    if not ent:
        return False
    # Check expiry
    from datetime import datetime, timezone
    expires = ent.get("expires_date")
    if expires is None:
        return True  # lifetime
    try:
        exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        return exp_dt > datetime.now(timezone.utc)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Webhook sync
# ---------------------------------------------------------------------------

def handle_webhook(payload: dict, company_id: int | None = None) -> dict:
    """
    Process a RevenueCat server notification webhook event.
    Stores event in IntegrationEvent for audit; never blocks on failure.
    """
    event  = payload.get("event", {})
    etype  = event.get("type", "unknown")
    app_uid = event.get("app_user_id")
    product = event.get("product_id")

    try:
        from extensions import db
        from models import IntegrationEvent
        ev = IntegrationEvent(
            company_id=company_id,
            provider="revenuecat",
            event_type=etype,
            external_id=app_uid,
            payload_json=json.dumps({
                "type": etype,
                "app_user_id": app_uid,
                "product_id": product,
                "period_type": event.get("period_type"),
                "expiration_at_ms": event.get("expiration_at_ms"),
            }),
            status="processed",
        )
        db.session.add(ev)
        db.session.commit()
        return {"ok": True, "event_type": etype}
    except Exception as exc:
        logger.error("RevenueCat webhook store error: %s", exc)
        return {"ok": False, "reason": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _secret_key():
    try:
        from services.provider_config import get_provider_config
        # REVENUECAT_API_KEY first, then legacy REVENUECAT_SECRET_KEY alias
        return (get_provider_config("revenuecat", "platform", "api_key") or
                get_provider_config("revenuecat", "platform", "secret_key"))
    except Exception:
        return None


def _headers(key: str) -> dict:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Platform": "web",
    }
