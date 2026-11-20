"""Centralized Twilio outbound-traffic safety gate.

Every live send boundary (SMS, voice, account/webhook configuration) must
obtain its Twilio REST client via get_twilio_client() rather than
constructing twilio.rest.Client directly, so LUXIT_TWILIO_MODE is enforced
in exactly one place instead of being scattered across every call site.

Read-only diagnostic calls (e.g. an integration health check that fetches
account metadata) are intentionally left ungated -- they never reach a
customer device and blocking them would break legitimate admin UI features
for no safety benefit.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

MODE_DISABLED = "disabled"
MODE_LIVE = "live"
# Staging/QA only: SMS/MMS to the E.164 numbers in LUXIT_TWILIO_ALLOWED_RECIPIENTS
# and nothing else (every other Twilio call is blocked exactly as in disabled).
MODE_ALLOWLIST = "allowlist"
_VALID_MODES = {MODE_DISABLED, MODE_LIVE, MODE_ALLOWLIST}


class TwilioSendBlockedError(Exception):
    """Raised when outbound Twilio traffic is attempted while
    LUXIT_TWILIO_MODE=disabled.

    Deliberately a plain Exception with a `.status` attribute in the
    400-499 range: every existing send call site already classifies
    arbitrary Twilio SDK exceptions into retryable/terminal/ambiguous by
    inspecting `.status`, so this maps to "terminal" (a disabled gate will
    not resolve itself on retry) through that same, unmodified logic.
    """

    status = 403
    code = "twilio_gate_disabled"

    def __init__(self, boundary: str):
        super().__init__(
            f"Twilio outbound traffic is disabled (LUXIT_TWILIO_MODE=disabled); "
            f"blocked at {boundary}."
        )


def resolve_twilio_mode() -> str:
    """Resolve the effective Twilio send mode.

    Fails closed (disabled) when the value is missing everywhere except
    Production, and on any malformed/unknown value everywhere, including
    Production. Never logs credentials -- only the (safe) raw mode string
    and app_env are logged, and only when the value is unrecognized.
    """
    app_env = (os.environ.get("APP_ENV") or os.environ.get("FLASK_ENV") or "").strip().lower()
    is_production = app_env == "production"

    raw = os.environ.get("LUXIT_TWILIO_MODE")
    if raw is None or raw.strip() == "":
        return MODE_LIVE if is_production else MODE_DISABLED

    normalized = raw.strip().lower()
    if normalized == MODE_ALLOWLIST and is_production:
        logger.warning("LUXIT_TWILIO_MODE=allowlist is not allowed in production; failing closed to %r.", MODE_DISABLED)
        return MODE_DISABLED
    if normalized in _VALID_MODES:
        return normalized

    logger.warning(
        "LUXIT_TWILIO_MODE=%r is not a recognized value (expected one of %s); "
        "failing closed to %r. app_env=%s",
        raw, sorted(_VALID_MODES), MODE_DISABLED, app_env or "unknown",
    )
    return MODE_DISABLED


class _BlockedTwilioClient:
    """Stands in for twilio.rest.Client while sends are disabled.

    Raises on any attribute access so a blocked send fails loudly and is
    classified the same way any other Twilio SDK error already is by
    calling code, rather than silently no-op'ing or returning a fake
    success.
    """

    def __init__(self, boundary: str):
        self._boundary = boundary

    def __getattr__(self, name):
        raise TwilioSendBlockedError(self._boundary)


def allowed_recipients() -> frozenset[str]:
    """E.164 numbers from LUXIT_TWILIO_ALLOWED_RECIPIENTS (comma/space separated)."""
    raw = os.environ.get("LUXIT_TWILIO_ALLOWED_RECIPIENTS") or ""
    return frozenset(p for p in raw.replace(",", " ").split() if p.startswith("+") and p[1:].isdigit())


class _AllowlistMessages:
    def __init__(self, real_client, boundary: str, allowed: frozenset[str]):
        self._real = real_client
        self._boundary = boundary
        self._allowed = allowed

    def create(self, **kwargs):
        to = str(kwargs.get("to") or "").strip()
        if to not in self._allowed:
            logger.info("Twilio allowlist mode refused a message to a non-allowlisted recipient at %s", self._boundary)
            raise TwilioSendBlockedError(f"{self._boundary} (recipient not in LUXIT_TWILIO_ALLOWED_RECIPIENTS)")
        return self._real.messages.create(**kwargs)

    def __getattr__(self, name):
        raise TwilioSendBlockedError(self._boundary)


class _AllowlistTwilioClient(_BlockedTwilioClient):
    """Exposes only messages.create() to allowlisted recipients. Calls, number /
    webhook configuration and every other API stay blocked, so a staging box
    can never reconfigure the shared Twilio account or reach a real customer."""

    def __init__(self, account_sid, auth_token, boundary: str, allowed: frozenset[str]):
        super().__init__(boundary)
        from twilio.rest import Client
        self.messages = _AllowlistMessages(Client(account_sid, auth_token), boundary, allowed)


def get_twilio_client(account_sid: str | None, auth_token: str | None, *, boundary: str = "unknown"):
    """Return a real twilio.rest.Client in live mode, or a client-shaped
    stand-in that blocks every call while LUXIT_TWILIO_MODE=disabled.

    `boundary` is a short, non-secret label identifying the call site
    (e.g. "twilio_sms.sendConversationSms") for diagnostics -- never pass
    credentials or message content here.
    """
    mode = resolve_twilio_mode()
    if mode == MODE_LIVE:
        from twilio.rest import Client
        return Client(account_sid, auth_token)
    if mode == MODE_ALLOWLIST:
        allowed = allowed_recipients()
        if allowed:
            return _AllowlistTwilioClient(account_sid, auth_token, boundary, allowed)
    return _BlockedTwilioClient(boundary)
