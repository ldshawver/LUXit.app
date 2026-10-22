"""Shared guard for applying Twilio SMS delivery-status callbacks.

Twilio's ``StatusCallback`` webhook is not guaranteed to arrive in order —
retries, provider-side re-delivery, or plain network reordering can deliver
an earlier status (e.g. ``queued``/``sent``) *after* a later one
(``delivered``/``failed``/``undelivered``) has already been applied. Every
write path that persists a delivery status from that webhook must apply this
guard first, or a stale callback can silently regress an already-resolved
message back to a non-terminal state -- producing a false "just sent" UI
state for a message Twilio already confirmed delivered or failed.

This module makes that one decision in one place; it does not touch send-time
status or any other business logic.
"""
from __future__ import annotations

# Twilio's documented outbound-message status progression. Values not listed
# here (a future Twilio status this code doesn't know about) are always
# treated as forward-safe -- this guard's job is to catch *known* regressions,
# never to block a legitimate new provider state.
_STATUS_RANK = {
    "accepted": 0,
    "queued": 0,
    "sending": 1,
    "sent": 2,
    "delivered": 3,
    "read": 4,
    "undelivered": 3,
    "failed": 3,
}

# "delivered", "undelivered", and "failed" share the top rank: Twilio can
# legitimately report one of these outcomes and later correct it to another
# (e.g. a delivery receipt problem discovered after an initial "delivered"),
# and that lateral correction must still be applied. What must never happen
# is regressing from any of these back down to an earlier, non-terminal
# status such as "queued" or "sent" -- that is always a stale/out-of-order
# callback, not a real correction.
TERMINAL_STATUSES = frozenset({"delivered", "read", "undelivered", "failed"})


def is_forward_status_transition(current_status: str | None, incoming_status: str | None) -> bool:
    """True if it is safe to overwrite ``current_status`` with ``incoming_status``.

    - An empty/identical incoming status is always a safe no-op.
    - A transition to an unrecognized status (a future Twilio value this
      guard doesn't know about) is always allowed through.
    - Otherwise, apply it unless it is a documented regression: a known
      earlier status arriving after a known later one (out-of-order/stale
      webhook delivery).
    """
    current = (current_status or "").lower()
    incoming = (incoming_status or "").lower()
    if not incoming or incoming == current:
        return True
    if incoming not in _STATUS_RANK or current not in _STATUS_RANK:
        return True
    return _STATUS_RANK[incoming] >= _STATUS_RANK[current]
