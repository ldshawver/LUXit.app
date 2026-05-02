"""LUXit Stripe Billing service — Checkout, Customer Portal, tier mapping.

Single source of truth for:
  - Mapping Stripe ``lookup_key`` → (billing_tier, max_team_members)
  - Resolving prices by lookup_key (never trust client-supplied price IDs)
  - Computing seat totals across base subscription + add-on items
  - Default grace-period length on payment failure
"""
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier configuration
# ---------------------------------------------------------------------------
# lookup_key  →  (billing_tier, base_seats or None for unlimited)
TIER_BY_LOOKUP_KEY = {
    "luxit_starter_monthly":      ("starter",      5),
    "luxit_professional_monthly": ("professional", None),  # unlimited
}

# Per-account add-on: each unit grants one additional team seat.
ADDITIONAL_ACCOUNT_LOOKUP_KEY = "luxit_additional_account_monthly"

# Metered subscription item: contacts above the per-tier ``INCLUDED_CONTACTS``
# allowance are reported as usage records on this price (action="set").
CONTACTS_USAGE_LOOKUP_KEY = "luxit_contacts_usage_monthly"

# Per-tier included contact allowance. Anything beyond this is overage.
INCLUDED_CONTACTS_BY_TIER = {
    "starter":      2_500,
    "professional": 50_000,
}

# One-time onboarding/setup fee — a non-recurring price attached as a second
# line item on the very first subscription Checkout. Charged exactly once per
# company; subsequent renewals only bill the recurring subscription price.
SETUP_FEE_LOOKUP_KEY = "luxit_setup_fee_once"

# Lookup keys that can never start a Checkout Session on their own. They are
# either applied through the Customer Portal (seat add-on), only valid as a
# secondary line item on a real subscription (one-time setup fee), or are
# metered usage prices that ride along an existing subscription
# (contacts overage).
NON_PRIMARY_LOOKUP_KEYS = {
    ADDITIONAL_ACCOUNT_LOOKUP_KEY,
    SETUP_FEE_LOOKUP_KEY,
    CONTACTS_USAGE_LOOKUP_KEY,
}

# All lookup keys we recognise — anything else is rejected at checkout.
ALL_KNOWN_LOOKUP_KEYS = (
    set(TIER_BY_LOOKUP_KEY.keys())
    | {ADDITIONAL_ACCOUNT_LOOKUP_KEY, SETUP_FEE_LOOKUP_KEY, CONTACTS_USAGE_LOOKUP_KEY}
)

# Days of access after a failed invoice payment before the company is suspended.
DEFAULT_GRACE_DAYS = int(os.environ.get("BILLING_GRACE_DAYS", "14"))

# Customer-facing return / cancel URLs (overridable via env for staging).
DEFAULT_BASE_URL = os.environ.get("LUXIT_PUBLIC_BASE_URL", "https://luxit.app").rstrip("/")
DEFAULT_SUCCESS_URL = f"{DEFAULT_BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
DEFAULT_CANCEL_URL  = f"{DEFAULT_BASE_URL}/billing/cancel"
DEFAULT_PORTAL_RETURN_URL = f"{DEFAULT_BASE_URL}/app/billing"


# ---------------------------------------------------------------------------
# SDK helpers
# ---------------------------------------------------------------------------
def get_stripe():
    """Lazy-import the Stripe SDK and configure it with the secret key.

    Raises ``RuntimeError`` if ``STRIPE_SECRET_KEY`` is not configured so
    callers can return a clean 503 instead of a stack trace.
    """
    secret = os.environ.get("STRIPE_SECRET_KEY")
    if not secret:
        raise RuntimeError("STRIPE_SECRET_KEY not configured")
    import stripe  # type: ignore
    stripe.api_key = secret
    return stripe


def resolve_price_by_lookup_key(lookup_key: str):
    """Look up an active Stripe ``Price`` by ``lookup_key``.

    Raises ``ValueError`` if no active price exists for that key. We never
    accept a price ID from the frontend — the lookup_key allows the dashboard
    to rotate prices without code changes while keeping plans canonical.
    """
    if lookup_key not in ALL_KNOWN_LOOKUP_KEYS:
        raise ValueError(f"Unknown lookup_key: {lookup_key}")
    stripe = get_stripe()
    res = stripe.Price.list(
        lookup_keys=[lookup_key],
        active=True,
        limit=1,
        expand=["data.product"],
    )
    if not res.data:
        raise ValueError(f"No active Stripe price found for lookup_key={lookup_key}")
    return res.data[0]


def tier_for_lookup_key(lookup_key: str) -> Tuple[str, Optional[int]]:
    """Return ``(billing_tier, base_seats_or_None)`` for a given lookup_key."""
    return TIER_BY_LOOKUP_KEY.get(lookup_key, ("custom", None))


def compute_seats_from_subscription(sub_obj: dict) -> Tuple[str, Optional[int], Optional[str]]:
    """Walk a Stripe Subscription's items to derive (tier, total_seats, primary_lookup_key).

    Combines the base plan's seat allowance with any
    ``luxit_additional_account_monthly`` add-on quantity.

    Returns ``(tier, max_team_members_or_None, primary_lookup_key)``. When the
    base tier is unlimited (Professional), seats is ``None`` regardless of
    add-on quantity.
    """
    items = ((sub_obj or {}).get("items", {}) or {}).get("data", []) or []
    base_tier = "custom"
    base_seats: Optional[int] = None
    primary_lookup: Optional[str] = None
    addon_seats = 0
    saw_unlimited = False

    for item in items:
        price = item.get("price", {}) or {}
        lk = price.get("lookup_key")
        qty = item.get("quantity") or 1
        if lk == ADDITIONAL_ACCOUNT_LOOKUP_KEY:
            addon_seats += int(qty)
        elif lk in TIER_BY_LOOKUP_KEY:
            tier, seats = TIER_BY_LOOKUP_KEY[lk]
            base_tier = tier
            primary_lookup = lk
            if seats is None:
                saw_unlimited = True
            else:
                base_seats = seats

    if saw_unlimited:
        return base_tier, None, primary_lookup
    if base_seats is None:
        return base_tier, None, primary_lookup
    return base_tier, base_seats + addon_seats, primary_lookup


def included_contacts_for_tier(tier: Optional[str]) -> Optional[int]:
    """Return the per-tier included-contacts allowance, or ``None`` for unknown tiers."""
    if not tier:
        return None
    return INCLUDED_CONTACTS_BY_TIER.get(tier)


def find_contacts_usage_item_id(sub_obj: dict) -> Optional[str]:
    """Walk a Stripe Subscription's items and return the ``si_…`` id of the
    metered contacts-usage line, if any.

    Used by the webhook handlers to persist
    ``Company.stripe_contact_usage_subscription_item_id`` so later usage
    reporting calls don't need a round-trip to Stripe.
    """
    items = ((sub_obj or {}).get("items", {}) or {}).get("data", []) or []
    for item in items:
        price = item.get("price", {}) or {}
        if price.get("lookup_key") == CONTACTS_USAGE_LOOKUP_KEY:
            return item.get("id")
    return None


def compute_contacts_overage(contacts_used: int, included_contacts: Optional[int]) -> int:
    """Return ``max(0, contacts_used - included_contacts)``.

    A ``None`` allowance is treated as no overage (e.g. enterprise/custom
    plans). Negative inputs are clamped to zero so we never report negative
    usage to Stripe.
    """
    used = max(0, int(contacts_used or 0))
    if included_contacts is None:
        return 0
    return max(0, used - int(included_contacts))


def report_contact_usage(company, *, now: Optional[datetime] = None):
    """Push a ``set``-action usage record to Stripe for one company's contact overage.

    Uses ``action="set"`` (not increment) so re-running this for the same
    period is idempotent and cannot double-bill. Persists
    ``last_reported_contact_usage`` and ``last_usage_reported_at`` on the
    company so callers can audit cadence.

    Returns a dict ``{"reported": bool, "quantity": int, "subscription_item": str|None,
    "skipped_reason": str|None}``. Never raises for the common "no usage item
    configured" case — it logs and returns ``reported=False`` so a daily job
    can scan all tenants without fatally aborting.
    """
    from extensions import db as _db
    item_id = getattr(company, "stripe_contact_usage_subscription_item_id", None)
    if not item_id:
        # Even when we can't push to Stripe, refresh the locally-cached
        # overage so the billing UI stays accurate.
        local_overage = compute_contacts_overage(
            getattr(company, "contacts_used", 0) or 0,
            getattr(company, "included_contacts", None),
        )
        try:
            company.contacts_overage = local_overage
            _db.session.commit()
        except Exception:
            _db.session.rollback()
        logger.info("report_contact_usage: company %s has no usage subscription item — skipping (local_overage=%s)",
                    getattr(company, "id", "?"), local_overage)
        return {"reported": False, "quantity": local_overage, "subscription_item": None,
                "skipped_reason": "no_usage_subscription_item"}

    overage = compute_contacts_overage(
        getattr(company, "contacts_used", 0) or 0,
        getattr(company, "included_contacts", None),
    )
    # Always persist the locally-computed overage so the UI matches.
    company.contacts_overage = overage

    stripe = get_stripe()
    ts = int((now or datetime.utcnow()).timestamp())
    # action="set" overwrites any previous record in the current period.
    # This avoids double-counting across daily/manual triggers.
    stripe.SubscriptionItem.create_usage_record(
        item_id,
        quantity=overage,
        timestamp=ts,
        action="set",
    )
    company.last_reported_contact_usage = overage
    company.last_usage_reported_at = datetime.utcfromtimestamp(ts)
    try:
        _db.session.commit()
    except Exception:
        _db.session.rollback()
        raise
    logger.info("report_contact_usage: company=%s item=%s quantity=%s action=set",
                getattr(company, "id", "?"), item_id, overage)
    return {"reported": True, "quantity": overage, "subscription_item": item_id,
            "skipped_reason": None}


def grace_period_end() -> datetime:
    """UTC timestamp marking the end of the dunning grace window."""
    return datetime.utcnow() + timedelta(days=DEFAULT_GRACE_DAYS)


def stripe_ts_to_dt(ts):
    """Convert a Stripe Unix timestamp (or None) to a UTC ``datetime``."""
    if ts is None:
        return None
    try:
        return datetime.utcfromtimestamp(int(ts))
    except (ValueError, TypeError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Checkout / Portal session creators
# ---------------------------------------------------------------------------
def create_checkout_session(
    lookup_key: str,
    *,
    company_id,
    user_id=None,
    customer_id: Optional[str] = None,
    customer_email: Optional[str] = None,
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
    quantity: int = 1,
    include_setup_fee: bool = False,
):
    """Create a Stripe Checkout Session in subscription mode.

    Resolves ``lookup_key`` → price server-side; the frontend never supplies
    price IDs or amounts. Attaches ``client_reference_id`` and ``metadata``
    so the webhook can reconcile the resulting subscription back to a tenant.
    """
    if lookup_key not in ALL_KNOWN_LOOKUP_KEYS:
        raise ValueError(f"Unknown lookup_key: {lookup_key}")
    # Add-on / setup-fee lookup keys cannot start their own subscription —
    # the add-on belongs on the existing subscription via the Customer Portal,
    # and the setup fee is only valid as a secondary line item attached to a
    # real tier checkout (see ``include_setup_fee`` below).
    if lookup_key == ADDITIONAL_ACCOUNT_LOOKUP_KEY:
        raise ValueError(
            "luxit_additional_account_monthly cannot start a new subscription; "
            "use the Customer Portal to add seats to your existing subscription"
        )
    if lookup_key == SETUP_FEE_LOOKUP_KEY:
        raise ValueError(
            "luxit_setup_fee_once is a one-time fee and cannot be the primary "
            "lookup_key; pass include_setup_fee=True alongside a tier lookup_key"
        )
    if lookup_key == CONTACTS_USAGE_LOOKUP_KEY:
        raise ValueError(
            "luxit_contacts_usage_monthly is a metered usage price; it is "
            "added to the subscription automatically and cannot start its own"
        )

    stripe = get_stripe()
    price = resolve_price_by_lookup_key(lookup_key)

    line_items = [{"price": price.id, "quantity": int(quantity)}]
    setup_fee_included = False

    # Optionally attach the one-time setup fee as a second line item. Stripe
    # Checkout in subscription mode permits mixing a non-recurring price with
    # the recurring subscription price; the one-off charge appears on the
    # first invoice only and never recurs on renewal.
    if include_setup_fee:
        setup_price = resolve_price_by_lookup_key(SETUP_FEE_LOOKUP_KEY)
        line_items.append({"price": setup_price.id, "quantity": 1})
        setup_fee_included = True

    kwargs = {
        "mode": "subscription",
        "line_items": line_items,
        "automatic_tax": {"enabled": True},
        "success_url": success_url or DEFAULT_SUCCESS_URL,
        "cancel_url":  cancel_url  or DEFAULT_CANCEL_URL,
        "client_reference_id": str(company_id) if company_id is not None else None,
        "metadata": {
            "company_id":         str(company_id) if company_id is not None else "",
            "user_id":            str(user_id)    if user_id    is not None else "",
            "lookup_key":         lookup_key,
            "app":                "luxit",
            # Authoritative flag the webhook reads to decide whether to mark
            # ``company.setup_fee_paid`` on checkout.session.completed.
            "include_setup_fee":  "true" if setup_fee_included else "false",
        },
        # Carry the same metadata onto the resulting subscription so future
        # webhook events (sub.updated / sub.deleted) can reconcile too. The
        # setup fee flag is intentionally NOT mirrored here — it's a session-
        # level concern, not a subscription-level one.
        "subscription_data": {
            "metadata": {
                "company_id":  str(company_id) if company_id is not None else "",
                "user_id":     str(user_id)    if user_id    is not None else "",
                "lookup_key":  lookup_key,
                "app":         "luxit",
            },
        },
    }
    if customer_id:
        kwargs["customer"] = customer_id
    elif customer_email:
        kwargs["customer_email"] = customer_email

    session = stripe.checkout.Session.create(**kwargs)
    return session


def create_billing_portal_session(customer_id: str, *, return_url: Optional[str] = None):
    """Create a Stripe Customer Portal session for an existing customer."""
    if not customer_id:
        raise ValueError("customer_id is required")
    stripe = get_stripe()
    return stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url or DEFAULT_PORTAL_RETURN_URL,
    )
