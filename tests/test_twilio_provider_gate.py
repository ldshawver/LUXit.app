"""Tests for the centralized Twilio outbound safety gate (LUXIT_TWILIO_MODE).

Integration-level proof that inbound webhook processing, persistence, and
automation decisions still work while the gate is disabled lives in
tests/test_pwa_phone_system.py alongside the rest of the inbound-SMS suite
(see test_disabled_twilio_mode_blocks_send_but_inbound_processing_still_works
and test_stop_start_help_unaffected_by_disabled_twilio_mode there).
"""
import logging
import re
from pathlib import Path

import pytest

from services import twilio_gate


def test_disabled_mode_blocks_every_call_on_the_returned_client(monkeypatch):
    """A: disabled => no external Twilio API send occurs. The client-shaped
    stand-in raises on first use instead of ever reaching twilio.rest.Client."""
    monkeypatch.setenv("LUXIT_TWILIO_MODE", "disabled")
    client = twilio_gate.get_twilio_client("ACfake", "tokfake", boundary="test.boundary")

    with pytest.raises(twilio_gate.TwilioSendBlockedError):
        client.messages.create(body="hi", to="+15550001111", from_="+15550002222")
    with pytest.raises(twilio_gate.TwilioSendBlockedError):
        client.calls.create(to="+15550001111", from_="+15550002222", url="https://example.test")


def test_disabled_mode_blocked_error_carries_a_terminal_http_status():
    """The blocked-send exception must classify as terminal (400-499) so it
    plugs into the existing retryable/terminal/ambiguous classification
    logic in twilio_sms.sendConversationSms without any extra special-casing."""
    err = twilio_gate.TwilioSendBlockedError("some.boundary")
    assert 400 <= err.status < 500


def test_live_mode_returns_a_real_twilio_client(monkeypatch):
    """D: live => existing provider-send behavior is preserved -- a real,
    unmodified twilio.rest.Client is returned."""
    monkeypatch.setenv("LUXIT_TWILIO_MODE", "live")
    from twilio.rest import Client as RealClient

    client = twilio_gate.get_twilio_client("ACfake", "tokfake", boundary="test.boundary")
    assert isinstance(client, RealClient)


@pytest.mark.parametrize("app_env", ["development", "staging", ""])
def test_missing_mode_fails_closed_outside_production(monkeypatch, app_env):
    """E: missing LUXIT_TWILIO_MODE outside Production => fail closed."""
    monkeypatch.delenv("LUXIT_TWILIO_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    assert twilio_gate.resolve_twilio_mode() == twilio_gate.MODE_DISABLED


def test_missing_mode_defaults_live_only_in_production(monkeypatch):
    """Production must not silently disable a live paid product just
    because the operator forgot to set the variable -- but every other
    environment must default closed (see test above)."""
    monkeypatch.delenv("LUXIT_TWILIO_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    assert twilio_gate.resolve_twilio_mode() == twilio_gate.MODE_LIVE


@pytest.mark.parametrize("app_env", ["development", "staging", "production"])
def test_malformed_mode_fails_closed_everywhere_and_logs_safely(monkeypatch, app_env, caplog):
    """E: malformed/unknown value => fail closed everywhere, including
    Production, and log a diagnostic that never contains credentials."""
    monkeypatch.setenv("LUXIT_TWILIO_MODE", "on")  # not "disabled" or "live"
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.delenv("FLASK_ENV", raising=False)

    with caplog.at_level(logging.WARNING):
        mode = twilio_gate.resolve_twilio_mode()

    assert mode == twilio_gate.MODE_DISABLED
    assert any("LUXIT_TWILIO_MODE" in record.message for record in caplog.records)
    logged_text = " ".join(record.message for record in caplog.records)
    assert "auth_token" not in logged_text.lower()
    assert "account_sid" not in logged_text.lower()


def test_blocked_error_message_never_includes_credentials():
    err = twilio_gate.TwilioSendBlockedError("some.boundary")
    assert "ACfake" not in str(err)
    assert "tokfake" not in str(err)


# ── G: no individual send path bypasses the centralized gate ────────────────
#
# Static guard over the exact boundaries inventoried and migrated for this
# fix. A raw `= Client(` construction reappearing in any of these files
# means a new (or reverted) call site is bypassing services.twilio_gate.
# The one deliberate exception is the read-only integration health check
# (services/integrations/twilio_service.health_check), which only fetches
# account metadata, never sends anything, and is intentionally ungated.

_GATED_BOUNDARIES = {
    # rel_path: (expected get_twilio_client() uses, expected direct Client() constructions)
    "twilio_sms.py": (2, 0),                                  # _build_client, _auto_configure_twilio_webhook
    "inbox_pwa.py": (2, 0),                                   # place_outbound_call, dial_number
    "services/sms_service.py": (2, 0),                        # _ensure_twilio, _tenant_twilio_config
    # send_sms is gated; health_check's one direct construction is the sole
    # documented, intentional exception (read-only account fetch, never a
    # send -- see services/twilio_gate.py's module docstring).
    "services/integrations/twilio_service.py": (1, 1),
}


@pytest.mark.parametrize("rel_path,expected", sorted(_GATED_BOUNDARIES.items()))
def test_no_individual_send_path_bypasses_the_gate(rel_path, expected):
    expected_gate_uses, expected_direct_constructions = expected
    root = Path(__file__).resolve().parents[1]
    text = (root / rel_path).read_text()

    direct_constructions = re.findall(r"=\s*Client\(", text)
    assert len(direct_constructions) == expected_direct_constructions, (
        f"{rel_path} has {len(direct_constructions)} direct twilio.rest.Client() "
        f"construction(s), expected exactly {expected_direct_constructions} "
        f"(anything beyond the documented health_check exception must go "
        f"through services.twilio_gate.get_twilio_client())"
    )

    gate_uses = len(re.findall(r"get_twilio_client\(", text))
    assert gate_uses >= expected_gate_uses, (
        f"{rel_path} expected at least {expected_gate_uses} get_twilio_client() "
        f"call(s), found {gate_uses}"
    )
