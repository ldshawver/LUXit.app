"""Safety rules for forwarding inbound calls to an outside phone (e.g. a cell).

Forwarding is a plain <Dial><Number> from the voice webhook. Three things can
go wrong with that and are guarded here:

* Loops: a target that is one of our own Twilio numbers re-enters the voice
  webhook, which forwards again. Such targets are never dialed or saved, and
  a call that is itself the product of forwarding is not forwarded again.
* Carrier voicemail: a cell that does not pick up rolls to the carrier's
  voicemail, which Twilio sees as "answered" -- LUXit voicemail and the
  missed-call text would be skipped. Each forwarded leg is screened
  (``<Number url>`` -> "press 1"), and only a leg that pressed 1 counts as
  answered (CallEvent ``forward_accepted``).
* Caller identity: the forwarded leg shows the original caller when the
  caller's number is a real E.164 number, otherwise the business number.
"""
from __future__ import annotations

import os
import re

E164 = re.compile(r"^\+[1-9]\d{7,14}$")

FORWARD_ACCEPTED_EVENT = "forward_accepted"


def _number_key(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def own_number_keys() -> set[str]:
    """Every phone number that routes back into this platform's webhooks,
    across all tenants (a loop through another tenant is still a loop), plus
    LUXIT_OWN_NUMBERS (comma separated) for numbers this database does not
    hold, e.g. the other environment's numbers on a shared Twilio account."""
    from models import TwilioAccount, TwilioPhoneNumber

    keys = {_number_key(n) for (n,) in TwilioPhoneNumber.query.with_entities(TwilioPhoneNumber.phone_number).all()}
    keys |= {_number_key(n) for (n,) in TwilioAccount.query.with_entities(TwilioAccount.from_phone).all()}
    keys |= {_number_key(n) for n in os.environ.get("LUXIT_OWN_NUMBERS", "").split(",")}
    keys.discard("")
    return keys


def is_own_number(number: str | None) -> bool:
    key = _number_key(number)
    return bool(key) and key in own_number_keys()


def forward_targets(numbers) -> list[str]:
    """Dialable forward targets, in order: E.164 only, no duplicates, none of
    our own numbers."""
    own = own_number_keys()
    out: list[str] = []
    for number in numbers:
        number = (number or "").strip()
        if not E164.match(number) or _number_key(number) in own or number in out:
            continue
        out.append(number)
    return out


def is_forward_loop(from_number: str | None, forwarded_from: str | None, targets) -> bool:
    """True when this inbound call is already the product of forwarding: it
    comes from one of our own numbers, or the carrier reports it was forwarded
    by one of the forward targets (a cell whose no-answer forwarding points
    back at the business line). Such a call must not be forwarded again."""
    if is_own_number(from_number):
        return True
    key = _number_key(forwarded_from)
    return bool(key) and key in {_number_key(t) for t in targets}


def forward_caller_id(from_number: str | None, business_number: str) -> str:
    """Caller ID for a forwarded leg: the original caller when it is a real
    E.164 number (not anonymous/restricted), else the business number."""
    from_number = (from_number or "").strip()
    return from_number if E164.match(from_number) else business_number
