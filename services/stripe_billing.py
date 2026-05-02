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

# All lookup keys we recognise — anything else is rejected at checkout.
ALL_KNOWN_LOOKUP_KEYS = set(TIER_BY_LOOKUP_KEY.keys()) | {ADDITIONAL_ACCOUNT_LOOKUP_KEY}

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
):
    """Create a Stripe Checkout Session in subscription mode.

    Resolves ``lookup_key`` → price server-side; the frontend never supplies
    price IDs or amounts. Attaches ``client_reference_id`` and ``metadata``
    so the webhook can reconcile the resulting subscription back to a tenant.
    """
    if lookup_key not in ALL_KNOWN_LOOKUP_KEYS:
        raise ValueError(f"Unknown lookup_key: {lookup_key}")
    # The per-seat add-on must be added to the existing subscription as an
    # extra item (or quantity bump) — it is NOT a standalone subscription.
    # Forcing customers through the Customer Portal for seat changes keeps
    # billing on a single subscription record and avoids polluting the tier
    # detection logic with add-on-only sessions.
    if lookup_key == ADDITIONAL_ACCOUNT_LOOKUP_KEY:
        raise ValueError(
            "luxit_additional_account_monthly cannot start a new subscription; "
            "use the Customer Portal to add seats to your existing subscription"
        )

    stripe = get_stripe()
    price = resolve_price_by_lookup_key(lookup_key)

    kwargs = {
        "mode": "subscription",
        "line_items": [{"price": price.id, "quantity": int(quantity)}],
        "automatic_tax": {"enabled": True},
        "success_url": success_url or DEFAULT_SUCCESS_URL,
        "cancel_url":  cancel_url  or DEFAULT_CANCEL_URL,
        "client_reference_id": str(company_id) if company_id is not None else None,
        "metadata": {
            "company_id":  str(company_id) if company_id is not None else "",
            "user_id":     str(user_id)    if user_id    is not None else "",
            "lookup_key":  lookup_key,
            "app":         "luxit",
        },
        # Carry the same metadata onto the resulting subscription so future
        # webhook events (sub.updated / sub.deleted) can reconcile too.
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
