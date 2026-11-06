#!/usr/bin/env python3
"""Read-only Stripe preflight: is this account ready for LUXit checkout?

Run it with the key a staging (or production) instance will use, before the
first real checkout. It never creates, updates or deletes anything in Stripe.

Checks:
  1. the key is a test-mode key (``sk_test_``/``rk_test_``) unless ``--live``
     is passed; a live key without ``--live`` is refused before any API call,
  2. each lookup key the app knows (``services.stripe_billing``) resolves to
     exactly one active price of the right shape:
       tier + seat add-on  recurring, monthly, licensed
       contacts overage    recurring, monthly, metered
       setup fee           one-time
     and every price uses the same currency,
  3. Stripe Tax is active (checkout sessions set ``automatic_tax``; without
     it Stripe rejects every checkout),
  4. with ``--webhook-url``, an enabled webhook endpoint at that URL receives
     every event the webhook handler acts on.

Exit 0 = PASS, 1 = FAIL, 2 = refused (wrong key mode / no key). Prints one
``RESULT:`` line. Secrets are never printed.

Usage:
    STRIPE_SECRET_KEY=sk_test_... python scripts/acceptance/stripe_testmode_preflight.py \
        --webhook-url https://staging.luxit.app/api/stripe/webhook
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.stripe_billing import (  # noqa: E402
    ADDITIONAL_ACCOUNT_LOOKUP_KEY,
    CONTACTS_USAGE_LOOKUP_KEY,
    SETUP_FEE_LOOKUP_KEY,
    TIER_BY_LOOKUP_KEY,
)

# Every event type saas_mgmt.stripe_webhook acts on.
REQUIRED_EVENTS = {
    "checkout.session.completed",
    "customer.created",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}

LICENSED_MONTHLY = ("recurring", "month", "licensed")
EXPECTED_SHAPE = {
    **{k: LICENSED_MONTHLY for k in TIER_BY_LOOKUP_KEY},
    ADDITIONAL_ACCOUNT_LOOKUP_KEY: LICENSED_MONTHLY,
    CONTACTS_USAGE_LOOKUP_KEY: ("recurring", "month", "metered"),
    SETUP_FEE_LOOKUP_KEY: ("one_time", None, None),
}


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def key_mode(secret: str) -> str | None:
    for prefix, mode in (("sk_test_", "test"), ("rk_test_", "test"), ("sk_live_", "live"), ("rk_live_", "live")):
        if secret.startswith(prefix):
            return mode
    return None


def price_shape(price) -> tuple:
    recurring = _get(price, "recurring")
    if _get(price, "type") == "one_time" or not recurring:
        return ("one_time", None, None)
    return ("recurring", _get(recurring, "interval"), _get(recurring, "usage_type") or "licensed")


def check_prices(stripe) -> tuple[list[str], dict]:
    problems, found = [], {}
    for lookup_key, expected in EXPECTED_SHAPE.items():
        prices = list(stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=3).data)
        if not prices:
            problems.append(f"{lookup_key}: no active price")
            continue
        if len(prices) > 1:
            problems.append(f"{lookup_key}: {len(prices)} active prices (checkout would pick one arbitrarily)")
        price = prices[0]
        shape = price_shape(price)
        if shape != expected:
            problems.append(f"{lookup_key}: price {price.id} is {shape}, expected {expected}")
        found[lookup_key] = {"price": price.id, "currency": _get(price, "currency"),
                             "unit_amount": _get(price, "unit_amount"), "shape": "/".join(str(s) for s in shape if s),
                             "meter": _get(_get(price, "recurring"), "meter")}
    currencies = {v["currency"] for v in found.values()}
    if len(currencies) > 1:
        problems.append(f"prices use more than one currency: {sorted(currencies)}")
    return problems, found


METER_EVENT_NAME = os.environ.get("LUXIT_CONTACTS_METER_EVENT", "luxit_contacts_overage")


def check_meter(stripe, prices_found: dict) -> list[str]:
    """The metered contacts price bills from a Billing Meter (legacy usage
    records are gone). The meter must aggregate with "last": the app sends the
    current overage once a day, and "sum" would bill it every day again."""
    meters = [m for m in stripe.billing.Meter.list(limit=100).data if _get(m, "event_name") == METER_EVENT_NAME]
    if not meters:
        return [f"no Billing Meter with event_name {METER_EVENT_NAME!r} (contact overage can't be billed)"]
    meter = meters[0]
    problems = []
    if _get(meter, "status") != "active":
        problems.append(f"meter {_get(meter, 'id')} is {_get(meter, 'status')!r}, not 'active'")
    formula = _get(_get(meter, "default_aggregation"), "formula")
    if formula != "last":
        problems.append(f"meter {_get(meter, 'id')} aggregates with {formula!r}; must be 'last' (daily reports would add up)")
    usage = prices_found.get("luxit_contacts_usage_monthly")
    if usage and usage.get("meter") != _get(meter, "id"):
        problems.append(f"luxit_contacts_usage_monthly price {usage['price']} is not attached to meter {_get(meter, 'id')}")
    return problems


def check_tax(stripe) -> list[str]:
    try:
        settings = stripe.tax.Settings.retrieve()
    except Exception as exc:  # restricted key without tax read, or API error
        return [f"could not read Stripe Tax settings ({type(exc).__name__}); checkout uses automatic_tax"]
    if _get(settings, "status") != "active":
        return [f"Stripe Tax status is {_get(settings, 'status')!r}, not 'active'; every checkout would fail (automatic_tax)"]
    return []


def check_webhook(stripe, url: str) -> list[str]:
    endpoints = [e for e in stripe.WebhookEndpoint.list(limit=100).data if _get(e, "url") == url]
    if not endpoints:
        return [f"no webhook endpoint for {url}"]
    enabled = [e for e in endpoints if _get(e, "status") == "enabled"]
    if not enabled:
        return [f"webhook endpoint for {url} is disabled"]
    events = set(_get(enabled[0], "enabled_events") or [])
    if "*" in events:
        return []
    missing = sorted(REQUIRED_EVENTS - events)
    return [f"webhook endpoint for {url} does not receive: {', '.join(missing)}"] if missing else []


def run(stripe, secret: str, *, allow_live: bool, webhook_url: str | None) -> tuple[int, dict]:
    mode = key_mode(secret or "")
    if mode is None:
        return 2, {"refused": "STRIPE_SECRET_KEY is missing or not a Stripe secret/restricted key"}
    if mode == "live" and not allow_live:
        return 2, {"refused": "live-mode key; pass --live only for a deliberate read-only check of the live account"}
    stripe.api_key = secret
    problems, prices = check_prices(stripe)
    problems += check_meter(stripe, prices)
    problems += check_tax(stripe)
    if webhook_url:
        problems += check_webhook(stripe, webhook_url)
    return (0 if not problems else 1), {"mode": mode, "prices": prices, "problems": problems}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--webhook-url", help="the instance's public Stripe webhook URL")
    ap.add_argument("--live", action="store_true", help="allow a live-mode key (still read-only)")
    args = ap.parse_args(argv)
    import stripe
    code, report = run(stripe, os.environ.get("STRIPE_SECRET_KEY", ""), allow_live=args.live, webhook_url=args.webhook_url)
    for problem in report.get("problems", []):
        print("FAIL", problem)
    for key, info in report.get("prices", {}).items():
        print(f"ok   {key}: {info['price']} {info['shape']} {info['unit_amount']} {info['currency']}")
    if "refused" in report:
        print("REFUSED", report["refused"])
    print("RESULT:", json.dumps({"pass": code == 0, "mode": report.get("mode"),
                                 "problems": len(report.get("problems", [])), "refused": report.get("refused")}))
    return code


if __name__ == "__main__":
    sys.exit(main())
