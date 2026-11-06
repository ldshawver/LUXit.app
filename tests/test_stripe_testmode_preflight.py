"""scripts/acceptance/stripe_testmode_preflight.py against a faked Stripe SDK."""
import types

import pytest

from scripts.acceptance import stripe_testmode_preflight as pf

URL = "https://staging.luxit.app/api/stripe/webhook"


def _price(pid, *, recurring=None, currency="usd", amount=1000):
    return types.SimpleNamespace(id=pid, type="recurring" if recurring else "one_time",
                                 recurring=recurring, currency=currency, unit_amount=amount)


def _good_prices():
    monthly = {"interval": "month", "usage_type": "licensed"}
    return {
        "luxit_starter_monthly": [_price("price_s", recurring=monthly)],
        "luxit_professional_monthly": [_price("price_p", recurring=monthly)],
        "luxit_additional_account_monthly": [_price("price_a", recurring=monthly)],
        "luxit_contacts_usage_monthly": [_price("price_c", recurring={"interval": "month", "usage_type": "metered",
                                                                       "meter": "mtr_1"})],
        "luxit_setup_fee_once": [_price("price_f")],
    }


class FakeStripe:
    def __init__(self, prices=None, tax="active", endpoints=None, meters=None):
        self.api_key = None
        self.calls = []
        prices = _good_prices() if prices is None else prices
        endpoints = [{"url": URL, "status": "enabled", "enabled_events": sorted(pf.REQUIRED_EVENTS)}] if endpoints is None else endpoints
        fake = self

        class Price:
            @staticmethod
            def list(lookup_keys, active, limit):
                fake.calls.append(("Price.list", lookup_keys[0]))
                return types.SimpleNamespace(data=prices.get(lookup_keys[0], []))

        class Settings:
            @staticmethod
            def retrieve():
                fake.calls.append(("tax.Settings.retrieve",))
                return {"status": tax}

        class WebhookEndpoint:
            @staticmethod
            def list(limit):
                fake.calls.append(("WebhookEndpoint.list",))
                return types.SimpleNamespace(data=endpoints)

        meters = [{"id": "mtr_1", "event_name": pf.METER_EVENT_NAME, "status": "active",
                   "default_aggregation": {"formula": "last"}}] if meters is None else meters

        class Meter:
            @staticmethod
            def list(limit):
                fake.calls.append(("billing.Meter.list",))
                return types.SimpleNamespace(data=meters)

        self.Price, self.WebhookEndpoint = Price, WebhookEndpoint
        self.tax = types.SimpleNamespace(Settings=Settings)
        self.billing = types.SimpleNamespace(Meter=Meter)


def test_complete_test_account_passes():
    stripe = FakeStripe()
    code, report = pf.run(stripe, "sk_test_abc", allow_live=False, webhook_url=URL)
    assert code == 0, report["problems"]
    assert set(report["prices"]) == set(pf.EXPECTED_SHAPE)
    # read-only: only list/retrieve calls were made
    assert {c[0] for c in stripe.calls} == {"Price.list", "billing.Meter.list", "tax.Settings.retrieve",
                                            "WebhookEndpoint.list"}


@pytest.mark.parametrize("secret", ["", "pk_test_abc", "whsec_abc"])
def test_non_secret_key_refused(secret):
    stripe = FakeStripe()
    assert pf.run(stripe, secret, allow_live=False, webhook_url=None)[0] == 2
    assert stripe.calls == [] and stripe.api_key is None


def test_live_key_refused_before_any_call():
    stripe = FakeStripe()
    code, report = pf.run(stripe, "sk_live_abc", allow_live=False, webhook_url=None)
    assert code == 2 and "live-mode" in report["refused"]
    assert stripe.calls == [] and stripe.api_key is None


def test_wrong_shapes_duplicates_and_mixed_currency_reported():
    prices = _good_prices()
    prices["luxit_contacts_usage_monthly"] = [_price("price_c", recurring={"interval": "month", "usage_type": "licensed"})]
    prices["luxit_setup_fee_once"] = [_price("price_f", recurring={"interval": "month"})]
    prices["luxit_starter_monthly"].append(_price("price_s2", recurring={"interval": "month"}))
    prices["luxit_professional_monthly"] = [_price("price_p", recurring={"interval": "year"}, currency="eur")]
    del prices["luxit_additional_account_monthly"]
    code, report = pf.run(FakeStripe(prices=prices), "sk_test_abc", allow_live=False, webhook_url=None)
    joined = " | ".join(report["problems"])
    assert code == 1
    for needle in ("luxit_contacts_usage_monthly: price price_c", "luxit_setup_fee_once: price price_f",
                   "luxit_starter_monthly: 2 active prices", "luxit_professional_monthly: price price_p",
                   "luxit_additional_account_monthly: no active price", "more than one currency"):
        assert needle in joined


def test_inactive_tax_fails():
    code, report = pf.run(FakeStripe(tax="pending"), "sk_test_abc", allow_live=False, webhook_url=None)
    assert code == 1 and "automatic_tax" in report["problems"][0]


@pytest.mark.parametrize("endpoints,needle", [
    ([], "no webhook endpoint"),
    ([{"url": URL, "status": "disabled", "enabled_events": ["*"]}], "disabled"),
    ([{"url": URL, "status": "enabled", "enabled_events": ["checkout.session.completed"]}], "invoice.payment_failed"),
])
def test_webhook_problems(endpoints, needle):
    code, report = pf.run(FakeStripe(endpoints=endpoints), "sk_test_abc", allow_live=False, webhook_url=URL)
    assert code == 1 and needle in " ".join(report["problems"])


def test_wildcard_webhook_passes():
    endpoints = [{"url": URL, "status": "enabled", "enabled_events": ["*"]}]
    assert pf.run(FakeStripe(endpoints=endpoints), "rk_test_abc", allow_live=False, webhook_url=URL)[0] == 0


def test_required_events_match_the_webhook_handler():
    import inspect
    import saas_mgmt
    src = inspect.getsource(saas_mgmt)
    for ev in pf.REQUIRED_EVENTS:
        assert f'"{ev}"' in src, ev


@pytest.mark.parametrize("meters, needle", [
    ([], "no Billing Meter"),
    ([{"id": "mtr_1", "event_name": pf.METER_EVENT_NAME, "status": "active",
       "default_aggregation": {"formula": "sum"}}], "must be 'last'"),
    ([{"id": "mtr_1", "event_name": pf.METER_EVENT_NAME, "status": "inactive",
       "default_aggregation": {"formula": "last"}}], "not 'active'"),
    ([{"id": "mtr_other", "event_name": pf.METER_EVENT_NAME, "status": "active",
       "default_aggregation": {"formula": "last"}}], "not attached to meter"),
])
def test_contacts_meter_problems(meters, needle):
    code, report = pf.run(FakeStripe(meters=meters), "sk_test_abc", allow_live=False, webhook_url=None)
    assert code == 1 and any(needle in p for p in report["problems"]), report["problems"]


def test_meter_event_name_matches_the_app():
    from services.stripe_billing import CONTACTS_METER_EVENT_NAME
    assert pf.METER_EVENT_NAME == CONTACTS_METER_EVENT_NAME
