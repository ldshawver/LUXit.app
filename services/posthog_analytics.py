"""
Thin shim — delegates to utils.posthog_client so existing call-sites keep working.
"""
from utils.posthog_client import track_event, identify_user, group_company, _get_client


def capture(distinct_id, event, properties=None):
    track_event(distinct_id, event, properties)


def identify(distinct_id, traits=None):
    identify_user(distinct_id, traits)


def group(distinct_id, group_type, group_key, group_properties=None):
    group_company(distinct_id, group_key)
