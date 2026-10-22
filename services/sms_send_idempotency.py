"""Idempotency guard for manual (operator-initiated) conversation SMS sends.

A browser/network retry of a manual send -- a double-tap on Send, a client
timeout that resubmits, an automatic fetch retry -- must never produce a
second real Twilio send. ``SMSOutboundIntent``/``SMSOutboundAttempt``
(``services/sms_outbox.py``) already solve exactly this problem for
inbound-triggered automated replies, but require an ``inbound_message_id``
there is none for a human-composed send; ``SmsSendIdempotencyRecord`` is the
equivalent durable claim for that case.

Concurrency model: the UNIQUE constraint on
``(company_id, idempotency_key)`` is the actual backstop, not an in-process
lock (multiple gunicorn workers, no shared memory). The first request to
INSERT a row wins the key and proceeds to call Twilio; a concurrent or
retried request for the same key hits a unique-violation, reads the winning
row instead, and returns its outcome -- never sends a second time. If the
winning request's process dies after Twilio accepted the message but before
the row is marked terminal, the row is left at ``status="sending"`` forever;
any later request for that key gets ``delivery_unknown`` rather than a
resend, because provider acceptance in that state is genuinely unknown.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from extensions import db
from models import SmsSendIdempotencyRecord

# How long an unkeyed (client did not supply an idempotency key) duplicate
# send of the identical text to the identical conversation is collapsed into
# one. Covers the common accidental-double-tap / timeout-and-resubmit case
# without a frontend change. A deliberate resend of the same words later is
# a new bucket and goes through normally.
_FALLBACK_DEDUP_WINDOW_SECONDS = 8


def derive_fallback_key(user_id: int | None, conversation_id: int, body: str,
                        window_seconds: int = _FALLBACK_DEDUP_WINDOW_SECONDS) -> str:
    bucket = int(time.time() // max(1, window_seconds))
    material = "\x1f".join((str(user_id or ""), str(conversation_id), body, str(bucket)))
    return "auto:" + hashlib.sha256(material.encode()).hexdigest()


def send_with_idempotency(*, company_id: int, user_id: int | None, conversation_id: int,
                          body: str, twilio_account, to_number: str | None = None,
                          is_auto_reply: bool = False, rule_id: int | None = None,
                          idempotency_key: str | None = None) -> dict:
    """Send exactly once for a given ``(company_id, idempotency_key)``.

    ``idempotency_key`` should be client-supplied (stable across a retry of
    the same logical action) when available; otherwise a short time-bucketed
    key is derived from (user, conversation, body) so accidental duplicates
    still collapse.
    """
    key = idempotency_key or derive_fallback_key(user_id, conversation_id, body)

    record = SmsSendIdempotencyRecord(
        company_id=company_id, user_id=user_id, conversation_id=conversation_id,
        idempotency_key=key, status="sending",
    )
    db.session.add(record)
    try:
        db.session.commit()
        claimed = True
    except IntegrityError:
        db.session.rollback()
        claimed = False

    if not claimed:
        existing = SmsSendIdempotencyRecord.query.filter_by(
            company_id=company_id, idempotency_key=key,
        ).first()
        if not existing or existing.status == "sending":
            return {
                "success": False, "status": "delivery_unknown", "idempotent": True,
                "error": "A send for this message is already in progress or its outcome is unknown; not resending.",
            }
        return {
            "success": existing.status == "sent",
            "status": existing.status,
            "idempotent": True,
            "sid": existing.twilio_sid,
            "error": existing.error_message if existing.status != "sent" else None,
        }

    # We hold the claim. Perform the real send exactly once.
    from twilio_sms import sendConversationSms
    result = sendConversationSms(
        conversation_id, body, to_number=to_number, twilio_account=twilio_account,
        is_auto_reply=is_auto_reply, rule_id=rule_id,
    )

    record = SmsSendIdempotencyRecord.query.filter_by(id=record.id).one()
    if result.get("success"):
        record.status = "sent"
        record.twilio_sid = result.get("sid")
    else:
        record.status = "failed"
        record.error_message = (result.get("error") or "")[:2000]
    record.updated_at = datetime.utcnow()
    db.session.commit()

    result = dict(result)
    result["idempotent"] = False
    return result
