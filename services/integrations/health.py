"""
Central integration health orchestrator.
Called by GET /api/integrations/health.
Never leaks credentials — only safe status strings.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

logger = logging.getLogger(__name__)

_PROVIDERS = ["twilio", "github", "outlook", "airtable", "revenuecat"]
_TIMEOUT   = 8  # seconds per provider


def check_all() -> dict:
    """Return health status for all providers. Safe — never raises."""
    results = {}
    checkers = {
        "twilio":     _check_twilio,
        "github":     _check_github,
        "outlook":    _check_outlook,
        "airtable":   _check_airtable,
        "revenuecat": _check_revenuecat,
    }

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fn): name for name, fn in checkers.items()}
        for future in as_completed(futures, timeout=_TIMEOUT + 2):
            name = futures[future]
            try:
                results[name] = future.result(timeout=_TIMEOUT)
            except FuturesTimeout:
                results[name] = {"status": "error", "detail": "health check timed out"}
            except Exception as exc:
                results[name] = {"status": "error", "detail": str(exc)[:100]}

    # Fill in any providers that didn't return (shouldn't happen)
    for p in _PROVIDERS:
        if p not in results:
            results[p] = {"status": "error", "detail": "no response"}

    _persist_statuses(results)
    return results


def check_one(provider: str) -> dict:
    """Check a single provider. Returns safe dict."""
    checkers = {
        "twilio":     _check_twilio,
        "github":     _check_github,
        "outlook":    _check_outlook,
        "airtable":   _check_airtable,
        "revenuecat": _check_revenuecat,
    }
    fn = checkers.get(provider)
    if not fn:
        return {"status": "error", "detail": f"unknown provider '{provider}'"}
    try:
        result = fn()
        _persist_one(provider, result)
        return result
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:100]}


# ---------------------------------------------------------------------------
# Per-provider checks (import lazily to avoid circular deps)
# ---------------------------------------------------------------------------

def _check_twilio():
    from services.integrations.twilio_service import health_check
    return health_check()


def _check_github():
    from services.integrations.github_service import health_check
    return health_check()


def _check_outlook():
    from services.integrations.outlook_service import health_check
    return health_check()


def _check_airtable():
    from services.integrations.airtable_service import health_check
    return health_check()


def _check_revenuecat():
    from services.integrations.revenuecat_service import health_check
    return health_check()


# ---------------------------------------------------------------------------
# Persist statuses to IntegrationConnection table
# ---------------------------------------------------------------------------

def _persist_statuses(results: dict):
    for provider, result in results.items():
        _persist_one(provider, result)


def _persist_one(provider: str, result: dict):
    try:
        from datetime import datetime, timezone
        from extensions import db
        from models import IntegrationConnection

        now = datetime.now(timezone.utc)
        conn = IntegrationConnection.query.filter_by(
            company_id=None, provider=provider
        ).first()

        status = result.get("status", "error")

        if not conn:
            conn = IntegrationConnection(
                company_id=None,
                provider=provider,
                status=status,
                last_tested_at=now,
            )
            db.session.add(conn)
        else:
            conn.status         = status
            conn.last_tested_at = now

        if status == "connected":
            conn.last_success_at = now
            conn.last_error      = None
        else:
            conn.last_error = result.get("detail", "")[:500]

        db.session.commit()
    except Exception as exc:
        logger.debug("_persist_one(%s) failed: %s", provider, exc)
