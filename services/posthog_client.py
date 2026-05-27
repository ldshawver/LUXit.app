"""
PostHog backend helper for LUXit — posthog SDK v7.x compatible.

posthog 7.x API changes from 3.x:
  capture(event, distinct_id=..., properties={})   <-- event is FIRST arg
  set(distinct_id=..., properties={})              <-- replaces identify()
  group_identify(group_type, group_key, properties={}, distinct_id=...)
"""
import os
import logging

logger = logging.getLogger(__name__)

# Suppress the noisy "You have to specify a personal_api_key to use feature
# flags" warning that the PostHog SDK emits every poll cycle (30 s) when no
# personal API key is configured.  We intentionally run without one and use
# fail-open logic instead, so this is expected noise, not an error.
logging.getLogger("posthog").setLevel(logging.ERROR)

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    api_key = os.getenv("POSTHOG_API_KEY", "")
    host    = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")
    if not api_key:
        return None
    try:
        from posthog import Posthog
        _client = Posthog(project_api_key=api_key, host=host)
        _client.debug = False
        # PostHog SDK sets self.log.setLevel(WARNING) inside __init__ — override it
        # here so the repetitive "no personal_api_key" poller warning is suppressed.
        import logging as _logging
        _logging.getLogger("posthog").setLevel(_logging.ERROR)
        logger.info("PostHog client ready (host=%s)", host)
    except Exception as exc:
        logger.warning("PostHog init failed: %s", exc)
        _client = None
    return _client


def track_event(distinct_id, event, properties=None):
    """Send a server-side event. Never raises — app must not crash if PostHog is down."""
    if not os.getenv("POSTHOG_API_KEY"):
        return
    c = _get_client()
    if not c:
        return
    try:
        c.capture(event, distinct_id=str(distinct_id), properties=properties or {})
        c.flush()
    except Exception as exc:
        logger.warning("PostHog track_event failed: %s", exc)


def identify_user(distinct_id, traits=None):
    """Attach traits to a user profile (v7: uses set())."""
    if not os.getenv("POSTHOG_API_KEY"):
        return
    c = _get_client()
    if not c:
        return
    try:
        c.set(distinct_id=str(distinct_id), properties=traits or {})
        c.flush()
    except Exception as exc:
        logger.warning("PostHog identify_user failed: %s", exc)


def group_company(distinct_id, company_id, company_name=None, plan=None):
    """Associate user with a company group."""
    if not os.getenv("POSTHOG_API_KEY"):
        return
    c = _get_client()
    if not c:
        return
    try:
        props = {}
        if company_name:
            props["name"] = company_name
        if plan:
            props["plan"] = plan
        c.group_identify("company", str(company_id), props, distinct_id=str(distinct_id))
        c.flush()
    except Exception as exc:
        logger.warning("PostHog group_company failed: %s", exc)
