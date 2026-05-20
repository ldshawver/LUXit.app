import os
import logging

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    api_key = os.environ.get("POSTHOG_API_KEY", "")
    host    = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
    if not api_key:
        return None
    try:
        from posthog import Posthog
        _client = Posthog(project_api_key=api_key, host=host)
        _client.debug = False
        logger.info("PostHog initialised (host=%s)", host)
    except Exception as exc:
        logger.warning("PostHog init failed: %s", exc)
        _client = None
    return _client


def capture(distinct_id, event, properties=None):
    c = _get_client()
    if not c:
        return
    try:
        c.capture(str(distinct_id), event, properties or {})
    except Exception as exc:
        logger.debug("PostHog capture error: %s", exc)


def identify(distinct_id, traits=None):
    c = _get_client()
    if not c:
        return
    try:
        c.identify(str(distinct_id), traits or {})
    except Exception as exc:
        logger.debug("PostHog identify error: %s", exc)


def group(distinct_id, group_type, group_key, group_properties=None):
    c = _get_client()
    if not c:
        return
    try:
        c.group_identify(group_type, str(group_key), group_properties or {})
        c.capture(str(distinct_id), "$groupidentify", {
            "$group_type": group_type,
            "$group_key": str(group_key),
        })
    except Exception as exc:
        logger.debug("PostHog group error: %s", exc)
