"""Inbound SMS campaign attribution, compliance, and keyword automation."""
from __future__ import annotations

from datetime import datetime, time

from extensions import db
from models import (
    Contact, MarketingAuditLog, Notification, SMSAutoReplyRule, SMSCampaign,
    SMSKeywordRule, SMSRecipient, TwilioConversation, User,
)

BUSINESS_START = time(9, 0)
BUSINESS_END = time(17, 0)


def _now():
    return datetime.utcnow()


def _is_business_hours(at: datetime | None = None) -> bool:
    at = at or _now()
    return at.weekday() < 5 and BUSINESS_START <= at.time() <= BUSINESS_END


def _tags_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _save_tags(contact: Contact, tags: list[str]) -> None:
    seen = []
    for tag in tags:
        if tag and tag not in seen:
            seen.append(tag)
    contact.tags = ",".join(seen)


def find_contact(company_id: int, phone: str) -> Contact | None:
    if not company_id or not phone:
        return None
    return Contact.query.filter_by(company_id=company_id, phone=phone).first()


def mark_opt_out(company_id: int, phone: str, conversation: TwilioConversation | None = None) -> None:
    contact = find_contact(company_id, phone)
    if contact:
        contact.is_subscribed = False
        tags = _tags_list(contact.tags)
        if "sms_opt_out" not in tags:
            tags.append("sms_opt_out")
        _save_tags(contact, tags)
    if conversation:
        conversation.is_opted_out = True
        conversation.sms_opt_out_at = _now()
    for recipient in _campaign_recipients_for_contact(company_id, contact):
        recipient.status = "opted_out"
        recipient.opted_out_at = _now()
    _audit(company_id, None, "sms_opt_out", "contact", contact.id if contact else None, {"phone": phone})


def mark_opt_in(company_id: int, phone: str, conversation: TwilioConversation | None = None) -> None:
    contact = find_contact(company_id, phone)
    if contact:
        contact.is_subscribed = True
        tags = [tag for tag in _tags_list(contact.tags) if tag != "sms_opt_out"]
        if "sms_consent" not in tags:
            tags.append("sms_consent")
        _save_tags(contact, tags)
    if conversation:
        conversation.is_opted_out = False
        conversation.sms_opt_in_at = _now()
    _audit(company_id, None, "sms_opt_in", "contact", contact.id if contact else None, {"phone": phone})


def attribute_inbound_reply(company_id: int, phone: str, body: str, conversation_id: int | None = None) -> SMSRecipient | None:
    contact = find_contact(company_id, phone)
    if not contact:
        return None
    recipient = (
        SMSRecipient.query
        .join(SMSCampaign, SMSCampaign.id == SMSRecipient.campaign_id)
        .filter(SMSRecipient.company_id == company_id, SMSRecipient.contact_id == contact.id)
        .filter(SMSCampaign.status.in_(["sending", "sent", "scheduled"]))
        .order_by(SMSRecipient.sent_at.desc().nullslast(), SMSRecipient.created_at.desc())
        .first()
    )
    if recipient:
        recipient.status = "replied"
        recipient.replied_at = _now()
        _audit(company_id, None, "sms_campaign_reply", "sms_campaign", recipient.campaign_id, {
            "contact_id": contact.id,
            "conversation_id": conversation_id,
            "body_preview": (body or "")[:80],
        })
    return recipient


def update_delivery_status(message_sid: str, status: str, error_code: str | None = None, error_message: str | None = None) -> SMSRecipient | None:
    if not message_sid:
        return None
    recipient = SMSRecipient.query.filter_by(provider_message_sid=message_sid).first()
    if not recipient:
        return None
    normalized = (status or "").lower()
    next_status = recipient.status
    if normalized in {"delivered", "sent", "failed", "undelivered"}:
        next_status = "failed" if normalized == "undelivered" else normalized
    next_error_code = str(error_code) if error_code else recipient.provider_error_code
    next_error_message = error_message if error_code else recipient.error_message
    if (
        recipient.status == next_status
        and recipient.provider_error_code == next_error_code
        and recipient.error_message == next_error_message
        and (normalized != "delivered" or recipient.delivered_at is not None)
    ):
        return recipient
    recipient.status = next_status
    if normalized == "delivered":
        recipient.delivered_at = _now()
    if error_code:
        recipient.provider_error_code = next_error_code
        recipient.error_message = next_error_message
        recipient.status = "failed"
    recipient.updated_at = _now()
    _audit(recipient.company_id, None, "sms_delivery_status", "sms_recipient", recipient.id, {
        "message_sid": message_sid,
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
    })
    return recipient


def process_keyword_rules(company_id: int, body: str, conversation: TwilioConversation, twilio_account=None):
    """Apply tenant keyword rules and return the reply text if a rule matched."""
    if not company_id or not body:
        return None
    contact = conversation.contact or find_contact(company_id, conversation.from_number)
    campaign_recipient = attribute_inbound_reply(company_id, conversation.from_number, body, conversation.id)
    campaign_id = campaign_recipient.campaign_id if campaign_recipient else None

    query = SMSKeywordRule.query.filter_by(company_id=company_id, is_active=True)
    rules = query.order_by(SMSKeywordRule.priority.asc(), SMSKeywordRule.id.asc()).all()
    for rule in rules:
        if rule.campaign_id and rule.campaign_id != campaign_id:
            continue
        if not _matches(rule, body):
            continue
        reply = rule.reply_message or "Thanks — we received your message."
        if not _is_business_hours() and rule.after_hours_message:
            reply = rule.after_hours_message
        if contact:
            _apply_contact_actions(contact, rule)
        if conversation:
            tags = conversation.tags or []
            if rule.tag_to_add and rule.tag_to_add not in tags:
                tags.append(rule.tag_to_add)
                conversation.tags = tags
        if rule.notify_admin:
            _notify_admin(company_id, f"SMS keyword {rule.keyword} used", f"{conversation.from_number}: {(body or '')[:120]}")
        _audit(company_id, rule.created_by_user_id, "sms_keyword_triggered", "sms_keyword_rule", rule.id, {
            "campaign_id": campaign_id,
            "conversation_id": conversation.id,
            "phone": conversation.from_number,
            "keyword": rule.keyword,
        })
        db.session.flush()
        return reply

    auto = _auto_reply(company_id, campaign_id)
    if auto:
        reply = auto.reply_message or "Thanks for texting us. A team member will follow up soon."
        if not _is_business_hours() and auto.after_hours_message:
            reply = auto.after_hours_message
        _audit(company_id, auto.created_by_user_id, "sms_auto_reply_triggered", "sms_auto_reply_rule", auto.id, {
            "campaign_id": campaign_id,
            "conversation_id": conversation.id,
        })
        return reply
    return None


def _matches(rule: SMSKeywordRule, body: str) -> bool:
    keyword = (rule.keyword or "").strip().lower()
    text = (body or "").strip().lower()
    if not keyword:
        return False
    match_type = (rule.match_type or "exact").lower()
    if match_type == "contains":
        return keyword in text
    if match_type in {"starts_with", "starts-with", "prefix"}:
        return text.startswith(keyword)
    return text == keyword


def _apply_contact_actions(contact: Contact, rule: SMSKeywordRule) -> None:
    tags = _tags_list(contact.tags)
    if rule.tag_to_add and rule.tag_to_add not in tags:
        tags.append(rule.tag_to_add)
    if rule.segment_to_add:
        contact.segment = rule.segment_to_add
    _save_tags(contact, tags)
    contact.is_subscribed = True


def _campaign_recipients_for_contact(company_id: int, contact: Contact | None):
    if not contact:
        return []
    return SMSRecipient.query.filter_by(company_id=company_id, contact_id=contact.id).all()


def _auto_reply(company_id: int, campaign_id: int | None):
    query = SMSAutoReplyRule.query.filter_by(company_id=company_id, is_active=True)
    if campaign_id:
        specific = query.filter_by(campaign_id=campaign_id).first()
        if specific:
            return specific
    return query.filter(SMSAutoReplyRule.campaign_id.is_(None)).first()


def _notify_admin(company_id: int, title: str, message: str) -> None:
    user = User.query.filter_by(default_company_id=company_id, is_admin=True).first()
    if not user:
        return
    db.session.add(Notification(
        user_id=user.id,
        company_id=company_id,
        title=title,
        message=message,
        category="sms_keyword",
        icon="message-square",
        link="/twilio/inbox",
        is_read=False,
    ))


def _audit(company_id: int, user_id: int | None, action: str, entity_type: str, entity_id: int | None, details: dict) -> None:
    db.session.add(MarketingAuditLog(
        company_id=company_id,
        created_by_user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    ))
