"""Durable, idempotent outbound SMS intents for inbound webhook effects."""
from __future__ import annotations

import hashlib
from datetime import datetime

from extensions import db
from models import SMSOutboundAttempt, SMSOutboundIntent, TwilioMessage


RESOLVED_INTENT_STATUSES = {"delivered", "failed_terminal", "delivery_unknown"}


def deterministic_intent_key(
    inbound_message_id: int,
    effect_type: str,
    conversation_id: int,
    to_number: str | None,
    body: str,
    rule_id: int | None,
) -> str:
    material = "\x1f".join((
        str(inbound_message_id),
        effect_type,
        str(conversation_id),
        to_number or "",
        body,
        str(rule_id or ""),
    ))
    return hashlib.sha256(material.encode()).hexdigest()


def enqueue_sms_intent(
    *,
    inbound_message_id: int,
    company_id: int,
    conversation_id: int,
    effect_type: str,
    to_number: str | None,
    body: str,
    is_auto_reply: bool = False,
    rule_id: int | None = None,
    idempotency_key: str | None = None,
) -> SMSOutboundIntent:
    key = idempotency_key or deterministic_intent_key(
        inbound_message_id, effect_type, conversation_id, to_number, body, rule_id
    )
    existing = SMSOutboundIntent.query.filter_by(idempotency_key=key).first()
    if existing:
        return existing
    intent = SMSOutboundIntent(
        company_id=company_id,
        inbound_message_id=inbound_message_id,
        conversation_id=conversation_id,
        idempotency_key=key,
        effect_type=effect_type,
        to_number=to_number,
        body=body,
        is_auto_reply=is_auto_reply,
        rule_id=rule_id,
        status="pending",
    )
    db.session.add(intent)
    db.session.flush()
    return intent


def deliver_intent(intent_id: int, *, twilio_account=None) -> dict:
    """Deliver one intent without holding a database lock across the API call."""
    intent = (
        SMSOutboundIntent.query.filter_by(id=intent_id)
        .with_for_update()
        .populate_existing()
        .one()
    )
    if intent.status in RESOLVED_INTENT_STATUSES:
        db.session.rollback()
        return {"success": intent.status == "delivered", "status": intent.status, "idempotent": True}
    active_attempt = SMSOutboundAttempt.query.filter_by(
        intent_id=intent.id, status="sending"
    ).first()
    if active_attempt:
        # The process may have died after provider acceptance. Without a
        # provider idempotency token, resending would be unsafe.
        intent.status = "delivery_unknown"
        intent.last_error_code = "provider_acceptance_unknown"
        active_attempt.status = "delivery_unknown"
        active_attempt.finished_at = datetime.utcnow()
        db.session.commit()
        return {"success": False, "status": "delivery_unknown"}

    attempt_number = (intent.attempt_count or 0) + 1
    attempt = SMSOutboundAttempt(
        intent_id=intent.id,
        attempt_number=attempt_number,
        attempt_key=f"{intent.idempotency_key}:{attempt_number}",
        status="sending",
    )
    intent.attempt_count = attempt_number
    intent.status = "sending"
    db.session.add(attempt)
    delivery_payload = {
        "conversation_id": intent.conversation_id,
        "body": intent.body,
        "to_number": intent.to_number,
        "is_auto_reply": intent.is_auto_reply,
        "rule_id": intent.rule_id,
    }
    db.session.commit()

    # No row/advisory locks are held during this network call.
    from twilio_sms import sendConversationSms
    result = sendConversationSms(
        delivery_payload["conversation_id"],
        delivery_payload["body"],
        to_number=delivery_payload["to_number"],
        twilio_account=twilio_account,
        is_auto_reply=delivery_payload["is_auto_reply"],
        rule_id=delivery_payload["rule_id"],
        persist_record=False,
        bypass_outbox=True,
    )

    intent = (
        SMSOutboundIntent.query.filter_by(id=intent_id)
        .with_for_update()
        .populate_existing()
        .one()
    )
    attempt = SMSOutboundAttempt.query.filter_by(
        intent_id=intent.id, attempt_number=attempt_number
    ).with_for_update().one()
    now = datetime.utcnow()
    attempt.finished_at = now
    if result.get("success"):
        provider_sid = result.get("sid")
        intent.status = "delivered"
        intent.provider_sid = provider_sid
        intent.delivered_at = now
        intent.last_error_code = intent.last_error_message = None
        attempt.status = "delivered"
        attempt.provider_sid = provider_sid
        attempt.provider_status = result.get("provider_status") or result.get("status")
        attempt.provider_response = {
            "provider_status": result.get("provider_status") or result.get("status"),
        }
        db.session.add(TwilioMessage(
            conversation_id=intent.conversation_id,
            company_id=intent.company_id,
            twilio_sid=provider_sid,
            direction="outbound",
            from_number=result.get("from_number"),
            to_number=intent.to_number,
            body=intent.body,
            status="sent",
            is_auto_reply=intent.is_auto_reply,
            rule_id=intent.rule_id,
            raw_payload={"outbound_intent_id": intent.id},
        ))
    else:
        delivery_class = result.get("delivery_class") or "ambiguous"
        status = {
            "retryable": "failed_retryable",
            "terminal": "failed_terminal",
            "ambiguous": "delivery_unknown",
        }.get(delivery_class, "delivery_unknown")
        intent.status = attempt.status = status
        intent.last_error_code = attempt.error_code = result.get("error_code") or delivery_class
        intent.last_error_message = attempt.error_message = result.get("error")
    db.session.commit()
    return {"success": intent.status == "delivered", "status": intent.status}


def deliver_inbound_intents(inbound_message_id: int, *, twilio_account=None) -> dict:
    ids = [
        row[0] for row in db.session.query(SMSOutboundIntent.id).filter(
            SMSOutboundIntent.inbound_message_id == inbound_message_id,
            SMSOutboundIntent.status.in_(["pending", "failed_retryable", "sending"]),
        ).order_by(SMSOutboundIntent.id.asc()).all()
    ]
    results = [deliver_intent(intent_id, twilio_account=twilio_account) for intent_id in ids]
    statuses = [
        row[0] for row in db.session.query(SMSOutboundIntent.status).filter(
            SMSOutboundIntent.inbound_message_id == inbound_message_id
        ).all()
    ]
    return {
        "results": results,
        "all_delivered": all(status == "delivered" for status in statuses),
        "has_retryable": any(status in {"pending", "failed_retryable"} for status in statuses),
        "has_terminal": any(status in {"failed_terminal", "delivery_unknown"} for status in statuses),
    }
