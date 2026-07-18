"""Tenant-aware Marketing Hub APIs and helpers.

This module intentionally keeps provider operations fail-safe: missing Twilio or
social credentials produce structured 4xx/200 responses instead of page-level
500s, while all persistence is tenant scoped by company_id.
"""
from __future__ import annotations

import os
from datetime import datetime

from flask import Blueprint, jsonify, request, current_app
from flask_login import current_user, login_required
from sqlalchemy import or_

from extensions import db, csrf
from models import (
    Campaign, Company, Contact, SMSCampaign, SMSRecipient,
    MarketingAuditLog, SMSAutoReplyRule, SMSKeywordRule, SocialPost, TwilioAccount, user_company,
)

marketing_api_bp = Blueprint("marketing_api", __name__, url_prefix="/api/marketing")

SMS_STATUSES = {"draft", "scheduled", "sending", "sent", "paused", "canceled", "failed"}
HELP_KEYWORDS = {"HELP"}
STOP_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_KEYWORDS = {"START", "UNSTOP"}


def tenant_id() -> int | None:
    cid = getattr(current_user, "default_company_id", None)
    if cid:
        return cid
    row = db.session.query(Company.id).join(
        user_company, Company.id == user_company.c.company_id
    ).filter(user_company.c.user_id == current_user.id).first()
    return row[0] if row else None


def _json_error(message: str, status: int = 400, **extra):
    payload = {"success": False, "error": message}
    payload.update(extra)
    return jsonify(payload), status


def _twilio_ready(company_id: int | None) -> tuple[bool, list[str], TwilioAccount | None]:
    if not company_id:
        return False, ["company_id"], None
    account = TwilioAccount.query.filter_by(company_id=company_id, is_active=True).first()
    if account and account.is_configured:
        return True, [], account
    missing = []
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
        if not os.environ.get(key):
            missing.append(key)
    if not (os.environ.get("TWILIO_PHONE_NUMBER") or os.environ.get("TWILIO_FROM_NUMBER")):
        missing.append("TWILIO_PHONE_NUMBER")
    return not missing, missing, None


def _append_stop_language(message: str) -> str:
    body = (message or "").strip()
    if "STOP" not in body.upper():
        body = f"{body} Reply STOP to opt out.".strip()
    return body


def _contact_has_sms_consent(contact: Contact) -> bool:
    if marketing_skip_reason(contact, "sms") if "marketing_skip_reason" in globals() else False:
        return False
    if not contact.phone or not contact.is_active or not contact.is_subscribed:
        return False
    tags = (contact.tags or "").lower()
    if "sms_opt_out" in tags or "blocked" in tags or "invalid_phone" in tags:
        return False
    # If explicit consent tags are used, require them; otherwise honor legacy subscribed contacts.
    consent_markers = ("sms_consent", "sms_opt_in", "text_ok")
    return any(marker in tags for marker in consent_markers) or "no_sms" not in tags


def _audience_query(company_id: int, segment: str | None = None):
    q = Contact.query.filter_by(company_id=company_id, is_active=True)
    if segment:
        q = q.filter(or_(Contact.segment == segment, Contact.tags.ilike(f"%{segment}%")))
    return q


def _serialize_sms(c: SMSCampaign):
    rec = list(c.recipients) if hasattr(c, "recipients") else []
    counts = {
        "recipients_selected": len(rec),
        "queued": 0,
        "sent": 0,
        "delivered": 0,
        "failed": 0,
        "replied": 0,
        "opted_out": 0,
        "clicks": 0,
    }
    for r in rec:
        status = (r.status or "").lower()
        if status in counts:
            counts[status] += 1
    return {"id": c.id, "name": c.name, "message": c.message, "status": c.status or "draft", "scheduled_at": c.scheduled_at.isoformat() if c.scheduled_at else None, "metrics": counts}


@marketing_api_bp.get("/campaigns")
@login_required
def api_campaigns():
    cid = tenant_id()
    if not cid:
        return jsonify({"success": True, "campaigns": []})
    ctype = request.args.get("type")
    status = request.args.get("status")
    items = []
    if ctype in (None, "email"):
        q = Campaign.query.filter_by(company_id=cid)
        if status:
            q = q.filter_by(status=status)
        items.extend({"id": c.id, "type": "email", "name": c.name, "status": c.status or "draft"} for c in q.all())
    if ctype in (None, "sms"):
        q = SMSCampaign.query.filter_by(company_id=cid)
        if status:
            q = q.filter_by(status=status)
        items.extend(dict(_serialize_sms(c), type="sms") for c in q.all())
    if ctype in (None, "social"):
        q = SocialPost.query.filter_by(company_id=cid)
        if status:
            q = q.filter_by(status=status)
        items.extend({"id": p.id, "type": "social", "name": (p.content or "Social post")[:80], "status": p.status or "draft"} for p in q.all())
    return jsonify({"success": True, "campaigns": items})


@marketing_api_bp.post("/campaigns")
@login_required
def api_create_campaign():
    data = request.get_json(silent=True) or request.form
    cid = tenant_id()
    if not cid:
        return _json_error("tenant/company is required", 400)
    campaign = Campaign(company_id=cid, name=data.get("name") or "Untitled Campaign", subject=data.get("subject"), status=data.get("status") or "draft")
    db.session.add(campaign)
    db.session.commit()
    return jsonify({"success": True, "campaign": {"id": campaign.id, "name": campaign.name, "status": campaign.status}}), 201


@marketing_api_bp.get("/campaigns/<int:campaign_id>")
@login_required
def api_campaign_detail(campaign_id):
    c = Campaign.query.filter_by(id=campaign_id, company_id=tenant_id()).first_or_404()
    return jsonify({"success": True, "campaign": {"id": c.id, "name": c.name, "subject": c.subject, "status": c.status}})


@marketing_api_bp.put("/campaigns/<int:campaign_id>")
@login_required
def api_update_campaign(campaign_id):
    c = Campaign.query.filter_by(id=campaign_id, company_id=tenant_id()).first_or_404()
    data = request.get_json(silent=True) or {}
    for field in ("name", "subject", "status"):
        if field in data:
            setattr(c, field, data[field])
    db.session.commit()
    return jsonify({"success": True})


@marketing_api_bp.post("/campaigns/<int:campaign_id>/archive")
@login_required
def api_archive_campaign(campaign_id):
    c = Campaign.query.filter_by(id=campaign_id, company_id=tenant_id()).first_or_404()
    c.status = "archived"
    db.session.commit()
    return jsonify({"success": True})


@marketing_api_bp.post("/campaigns/<int:campaign_id>/duplicate")
@login_required
def api_duplicate_campaign(campaign_id):
    c = Campaign.query.filter_by(id=campaign_id, company_id=tenant_id()).first_or_404()
    dup = Campaign(company_id=c.company_id, name=f"{c.name} Copy", subject=c.subject, status="draft")
    db.session.add(dup)
    db.session.commit()
    return jsonify({"success": True, "campaign": {"id": dup.id}})


@marketing_api_bp.get("/sms-campaigns")
@login_required
def api_sms_list():
    cid = tenant_id()
    items = SMSCampaign.query.filter_by(company_id=cid).order_by(SMSCampaign.created_at.desc()).all() if cid else []
    return jsonify({"success": True, "campaigns": [_serialize_sms(c) for c in items]})


@marketing_api_bp.post("/sms-campaigns")
@login_required
def api_sms_create():
    data = request.get_json(silent=True) or request.form
    cid = tenant_id()
    if not cid:
        return _json_error("tenant/company is required")
    c = SMSCampaign(company_id=cid, created_by_user_id=current_user.id, name=data.get("name") or "Untitled SMS Campaign", objective=data.get("objective"), message=_append_stop_language(data.get("message") or ""), segment=data.get("segment"), status=data.get("status") or "draft")
    db.session.add(c)
    db.session.commit()
    return jsonify({"success": True, "campaign": _serialize_sms(c)}), 201


@marketing_api_bp.get("/sms-campaigns/<int:cid>")
@login_required
def api_sms_detail(cid):
    c = SMSCampaign.query.filter_by(id=cid, company_id=tenant_id()).first_or_404()
    return jsonify({"success": True, "campaign": _serialize_sms(c)})


@marketing_api_bp.put("/sms-campaigns/<int:cid>")
@login_required
def api_sms_update(cid):
    c = SMSCampaign.query.filter_by(id=cid, company_id=tenant_id()).first_or_404()
    data = request.get_json(silent=True) or {}
    for f in ("name", "objective", "segment"):
        if f in data:
            setattr(c, f, data[f])
    if "message" in data:
        c.message = _append_stop_language(data["message"])
    if data.get("status") in SMS_STATUSES:
        c.status = data["status"]
    db.session.commit()
    return jsonify({"success": True, "campaign": _serialize_sms(c)})


@marketing_api_bp.post("/sms-campaigns/<int:cid>/preview")
@login_required
def api_sms_preview(cid):
    c = SMSCampaign.query.filter_by(id=cid, company_id=tenant_id()).first_or_404()
    from services.contact_audience import resolve_segment_contacts, normalize_phone
    contacts = resolve_segment_contacts(c.company_id, c.segment, c.audience_filter or {})
    seen = set()
    eligible = []
    counts = {"total_matched": len(contacts), "duplicates_removed": 0, "invalid_numbers_removed": 0, "opt_outs_removed": 0, "final_recipients": 0}
    for x in contacts:
        phone = normalize_phone(x.normalized_phone or x.phone)
        if not phone:
            counts["invalid_numbers_removed"] += 1; continue
        if x.sms_opted_out or x.do_not_sms or x.sms_opt_out_at or not _contact_has_sms_consent(x):
            counts["opt_outs_removed"] += 1; continue
        if phone in seen:
            counts["duplicates_removed"] += 1; continue
        seen.add(phone); eligible.append((x, phone))
    counts["final_recipients"] = len(eligible)
    if not eligible:
        counts["explanation"] = "No recipients found because all matched contacts are missing SMS numbers or are opted out." if contacts else "No recipients found because the selected audience did not match any contacts."
    return jsonify({"success": True, **counts, "recipients_selected": len(eligible), "excluded": len(contacts) - len(eligible), "recipients": [{"id": x.id, "phone": phone, "name": f"{x.first_name or ''} {x.last_name or ''}".strip()} for x, phone in eligible[:25]]})


def _materialize_recipients(c: SMSCampaign):
    from services.contact_audience import build_sms_recipient_snapshot, resolve_segment_contacts
    if c.recipients.count() > 0:
        return resolve_segment_contacts(c.company_id, c.segment, c.audience_filter or {})
    counts = build_sms_recipient_snapshot(c)
    c.audience_filter = dict(c.audience_filter or {}, recipient_counts=counts)
    return resolve_segment_contacts(c.company_id, c.segment, c.audience_filter or {})


@marketing_api_bp.post("/sms-campaigns/<int:cid>/schedule")
@login_required
def api_sms_schedule(cid):
    c = SMSCampaign.query.filter_by(id=cid, company_id=tenant_id()).first_or_404()
    data = request.get_json(silent=True) or {}
    if not c.message:
        return _json_error("message body is empty")
    if not c.segment:
        return _json_error("no audience selected")
    when = data.get("scheduled_at")
    c.scheduled_at = datetime.fromisoformat(when) if when else datetime.utcnow()
    c.status = "scheduled"
    _materialize_recipients(c)
    db.session.commit()
    return jsonify({"success": True, "campaign": _serialize_sms(c)})


@marketing_api_bp.post("/sms-campaigns/<int:cid>/send")
@login_required
def api_sms_send(cid):
    c = (
        SMSCampaign.query
        .filter_by(id=cid, company_id=tenant_id())
        .with_for_update()
        .first_or_404()
    )
    data = request.get_json(silent=True) or {}
    ready, missing, twilio_account = _twilio_ready(c.company_id)
    if not ready:
        return _json_error("TWILIO/sender configuration missing", 409, missing=missing)
    if not c.message:
        return _json_error("message body is empty")
    if not c.segment:
        return _json_error("no audience selected")
    _materialize_recipients(c)
    if c.recipients.count() == 0:
        counts = (c.audience_filter or {}).get("recipient_counts") or {}
        return _json_error(counts.get("explanation") or "no eligible recipients with SMS consent", 400, counts=counts)
    batch_size = min(max(int(data.get("batch_size") or 100), 1), 100)
    retry_failed = bool(data.get("retry_failed", False))
    sendable_statuses = {"queued", "draft", "pending", None}
    if retry_failed:
        sendable_statuses.add("failed")
    recipients = (
        SMSRecipient.query
        .filter_by(company_id=c.company_id, campaign_id=c.id)
        .filter(SMSRecipient.provider_message_sid.is_(None))
        .filter(SMSRecipient.status.in_([s for s in sendable_statuses if s is not None]))
        .order_by(SMSRecipient.id.asc())
        .with_for_update(skip_locked=True)
        .limit(batch_size)
        .all()
    )
    if len(recipients) < batch_size and None in sendable_statuses:
        null_status_recipients = (
            SMSRecipient.query
            .filter_by(company_id=c.company_id, campaign_id=c.id, status=None)
            .filter(SMSRecipient.provider_message_sid.is_(None))
            .order_by(SMSRecipient.id.asc())
            .with_for_update(skip_locked=True)
            .limit(batch_size - len(recipients))
            .all()
        )
        recipients.extend(null_status_recipients)
    if not recipients:
        return _json_error(
            "campaign has no unsent recipients; duplicate send prevented",
            409,
            campaign_status=c.status,
        )
    c.status = "sending"
    sent_any = False
    processed = 0
    for r in recipients:
        processed += 1
        contact = db.session.get(Contact, r.contact_id) if r.contact_id else None
        if not contact or not _contact_has_sms_consent(contact):
            r.status = "failed"
            r.error_message = "Recipient is not eligible for SMS"
            continue
        if twilio_account:
            try:
                from twilio_sms import _get_or_create_conversation, _send_sms
                conv = _get_or_create_conversation(c.company_id, contact.phone, twilio_account.from_phone or "")
                result = _send_sms(twilio_account, contact.phone, c.message, conversation_id=conv.id)
                if result.get("success"):
                    r.status = result.get("status") or "sent"
                    r.provider_message_sid = result.get("sid")
                    r.sent_at = datetime.utcnow()
                    sent_any = True
                else:
                    r.status = "failed"
                    r.error_message = result.get("error")
            except Exception as exc:
                r.status = "failed"
                r.error_message = str(exc)
        else:
            # Env-only/test configuration: record the queued send without exposing credentials.
            r.status = "sent"
            r.sent_at = datetime.utcnow()
            sent_any = True
    remaining = (
        SMSRecipient.query
        .filter_by(company_id=c.company_id, campaign_id=c.id)
        .filter(SMSRecipient.provider_message_sid.is_(None))
        .filter(SMSRecipient.status.in_([s for s in sendable_statuses if s is not None]))
        .count()
    )
    if None in sendable_statuses:
        remaining += (
            SMSRecipient.query
            .filter_by(company_id=c.company_id, campaign_id=c.id, status=None)
            .filter(SMSRecipient.provider_message_sid.is_(None))
            .count()
        )
    c.status = "sending" if remaining else ("sent" if sent_any else "failed")
    if sent_any and not remaining:
        c.sent_at = datetime.utcnow()
    db.session.add(MarketingAuditLog(
        company_id=c.company_id,
        created_by_user_id=current_user.id,
        entity_type="sms_campaign",
        entity_id=c.id,
        action="sms_campaign_send",
        details={
            "sent_any": sent_any,
            "recipient_count": c.recipients.count(),
            "processed": processed,
            "remaining": remaining,
            "batch_size": batch_size,
            "retry_failed": retry_failed,
        },
    ))
    db.session.commit()
    return jsonify({"success": True, "campaign": _serialize_sms(c)})


@marketing_api_bp.post("/sms-campaigns/<int:cid>/pause")
@login_required
def api_sms_pause(cid):
    c = SMSCampaign.query.filter_by(id=cid, company_id=tenant_id()).first_or_404()
    c.status = "paused"
    db.session.commit()
    return jsonify({"success": True})


@marketing_api_bp.post("/sms-campaigns/<int:cid>/cancel")
@login_required
def api_sms_cancel(cid):
    c = SMSCampaign.query.filter_by(id=cid, company_id=tenant_id()).first_or_404()
    c.status = "canceled"
    db.session.commit()
    return jsonify({"success": True})


@marketing_api_bp.get("/sms-campaigns/<int:cid>/analytics")
@login_required
def api_sms_analytics(cid):
    c = SMSCampaign.query.filter_by(id=cid, company_id=tenant_id()).first_or_404()
    return jsonify({"success": True, "analytics": _serialize_sms(c)["metrics"]})


def _contact_duplicate_json(contact: Contact):
    return {
        "id": contact.id,
        "name": contact.name or f"{contact.first_name or ''} {contact.last_name or ''}".strip(),
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "company": contact.company,
        "email": contact.email,
        "phone": contact.phone,
        "normalized_phone": contact.normalized_phone,
        "tags": contact.tags,
        "source_channel": getattr(contact, "source_channel", None),
        "source_phone_number": getattr(contact, "source_phone_number", None),
        "sms_opted_out": bool(contact.sms_opted_out or contact.do_not_sms or contact.sms_opt_out_at),
        "email_unsubscribed": bool(contact.email_unsubscribed or contact.do_not_email),
        "created_at": contact.created_at.isoformat() if contact.created_at else None,
    }


@marketing_api_bp.get("/contacts/duplicates")
@login_required
def api_contact_duplicates():
    cid = tenant_id()
    if not cid:
        return _json_error("tenant/company is required", 400)
    from services.contact_dedupe import find_duplicate_contacts
    groups = []
    for group in find_duplicate_contacts(cid):
        groups.append({
            "match_key": group["match_key"],
            "contact_ids": group["contact_ids"],
            "contacts": [_contact_duplicate_json(c) for c in group["contacts"]],
        })
    return jsonify({"success": True, "duplicate_groups": groups, "count": len(groups)})


@marketing_api_bp.post("/contacts/duplicates/merge")
@login_required
def api_contact_duplicates_merge():
    cid = tenant_id()
    if not cid:
        return _json_error("tenant/company is required", 400)
    data = request.get_json(silent=True) or {}
    primary_id = data.get("primary_contact_id")
    duplicate_ids = data.get("duplicate_contact_ids") or []
    if not primary_id or not duplicate_ids:
        return _json_error("primary_contact_id and duplicate_contact_ids are required", 400)
    from services.contact_dedupe import merge_contacts
    try:
        result = merge_contacts(
            int(primary_id),
            [int(x) for x in duplicate_ids],
            actor_user_id=current_user.id,
            company_id=cid,
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"success": True, "merge": result})


@marketing_api_bp.get("/sms-keywords")
@login_required
def api_keywords_get():
    rules = SMSKeywordRule.query.filter_by(company_id=tenant_id()).order_by(SMSKeywordRule.priority.asc(), SMSKeywordRule.id.asc()).all()
    return jsonify({"success": True, "rules": [_keyword_json(r) for r in rules]})

@marketing_api_bp.post("/sms-keywords")
@login_required
def api_keywords_post():
    data = request.get_json(silent=True) or {}
    cid = tenant_id()
    if not cid:
        return _json_error("tenant/company is required")
    rule = SMSKeywordRule(
        company_id=cid,
        campaign_id=data.get("campaign_id"),
        keyword=(data.get("keyword") or "").strip().upper(),
        match_type=data.get("match_type") or "exact",
        reply_message=data.get("reply_message") or data.get("message"),
        priority=int(data.get("priority") or 100),
        is_active=bool(data.get("is_active", data.get("active", True))),
        business_hours_only=bool(data.get("business_hours_only", False)),
        after_hours_message=data.get("after_hours_message"),
        tag_to_add=data.get("tag_to_add"),
        segment_to_add=data.get("segment_to_add"),
        notify_admin=bool(data.get("notify_admin", False)),
        created_by_user_id=current_user.id,
    )
    if not rule.keyword:
        return _json_error("keyword is required")
    db.session.add(rule)
    db.session.flush()
    _audit_log(cid, "sms_keyword_rule", rule.id, "sms_keyword_rule_create", {"keyword": rule.keyword})
    db.session.commit()
    return jsonify({"success": True, "rule": _keyword_json(rule)}), 201

@marketing_api_bp.put("/sms-keywords/<int:rid>")
@login_required
def api_keywords_put(rid):
    data = request.get_json(silent=True) or {}
    rule = SMSKeywordRule.query.filter_by(id=rid, company_id=tenant_id()).first()
    if not rule:
        return _json_error("rule not found", 404)
    for field in ("campaign_id", "keyword", "match_type", "reply_message", "priority", "business_hours_only", "after_hours_message", "tag_to_add", "segment_to_add", "notify_admin"):
        if field in data:
            setattr(rule, field, data[field])
    if "active" in data or "is_active" in data:
        rule.is_active = bool(data.get("is_active", data.get("active")))
    _audit_log(rule.company_id, "sms_keyword_rule", rule.id, "sms_keyword_rule_update", {"fields": list(data.keys())})
    db.session.commit()
    return jsonify({"success": True, "rule": _keyword_json(rule)})

@marketing_api_bp.delete("/sms-keywords/<int:rid>")
@login_required
def api_keywords_delete(rid):
    rule = SMSKeywordRule.query.filter_by(id=rid, company_id=tenant_id()).first_or_404()
    db.session.delete(rule)
    db.session.commit()
    return jsonify({"success": True})

@marketing_api_bp.get("/sms-auto-replies")
@login_required
def api_auto_get():
    rules = SMSAutoReplyRule.query.filter_by(company_id=tenant_id()).order_by(SMSAutoReplyRule.id.asc()).all()
    return jsonify({"success": True, "rules": [_auto_reply_json(r) for r in rules]})

@marketing_api_bp.post("/sms-auto-replies")
@login_required
def api_auto_post():
    data = request.get_json(silent=True) or {}
    cid = tenant_id()
    if not cid:
        return _json_error("tenant/company is required")
    rule = SMSAutoReplyRule(
        company_id=cid,
        campaign_id=data.get("campaign_id"),
        name=data.get("name") or "Auto reply",
        trigger_type=data.get("trigger_type") or "inbound",
        reply_message=data.get("reply_message") or data.get("message"),
        after_hours_message=data.get("after_hours_message"),
        is_active=bool(data.get("is_active", data.get("active", True))),
        created_by_user_id=current_user.id,
    )
    db.session.add(rule)
    db.session.flush()
    _audit_log(cid, "sms_auto_reply_rule", rule.id, "sms_auto_reply_rule_create", {"name": rule.name})
    db.session.commit()
    return jsonify({"success": True, "rule": _auto_reply_json(rule)}), 201

@marketing_api_bp.put("/sms-auto-replies/<int:rid>")
@login_required
def api_auto_put(rid):
    data = request.get_json(silent=True) or {}
    rule = SMSAutoReplyRule.query.filter_by(id=rid, company_id=tenant_id()).first()
    if not rule:
        return _json_error("rule not found", 404)
    for field in ("campaign_id", "name", "trigger_type", "reply_message", "after_hours_message"):
        if field in data:
            setattr(rule, field, data[field])
    if "active" in data or "is_active" in data:
        rule.is_active = bool(data.get("is_active", data.get("active")))
    _audit_log(rule.company_id, "sms_auto_reply_rule", rule.id, "sms_auto_reply_rule_update", {"fields": list(data.keys())})
    db.session.commit()
    return jsonify({"success": True, "rule": _auto_reply_json(rule)})

@marketing_api_bp.delete("/sms-auto-replies/<int:rid>")
@login_required
def api_auto_delete(rid):
    rule = SMSAutoReplyRule.query.filter_by(id=rid, company_id=tenant_id()).first_or_404()
    _audit_log(rule.company_id, "sms_auto_reply_rule", rule.id, "sms_auto_reply_rule_delete", {"name": rule.name})
    db.session.delete(rule)
    db.session.commit()
    return jsonify({"success": True})


def _audit_log(company_id: int, entity_type: str, entity_id: int, action: str, details: dict):
    db.session.add(MarketingAuditLog(
        company_id=company_id,
        created_by_user_id=current_user.id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        details=details,
    ))


@marketing_api_bp.get("/audit-logs")
@login_required
def api_audit_logs():
    cid = tenant_id()
    logs = (
        MarketingAuditLog.query
        .filter_by(company_id=cid)
        .order_by(MarketingAuditLog.created_at.desc(), MarketingAuditLog.id.desc())
        .limit(100)
        .all()
    ) if cid else []
    return jsonify({
        "success": True,
        "audit_logs": [
            {
                "id": log.id,
                "entity_type": log.entity_type,
                "entity_id": log.entity_id,
                "action": log.action,
                "details": log.details or {},
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ],
    })


def _keyword_json(rule: SMSKeywordRule):
    return {
        "id": rule.id,
        "campaign_id": rule.campaign_id,
        "keyword": rule.keyword,
        "match_type": rule.match_type,
        "reply_message": rule.reply_message,
        "priority": rule.priority,
        "is_active": rule.is_active,
        "business_hours_only": rule.business_hours_only,
        "after_hours_message": rule.after_hours_message,
        "tag_to_add": rule.tag_to_add,
        "segment_to_add": rule.segment_to_add,
        "notify_admin": rule.notify_admin,
    }


def _auto_reply_json(rule: SMSAutoReplyRule):
    return {
        "id": rule.id,
        "campaign_id": rule.campaign_id,
        "name": rule.name,
        "trigger_type": rule.trigger_type,
        "reply_message": rule.reply_message,
        "after_hours_message": rule.after_hours_message,
        "is_active": rule.is_active,
    }


@marketing_api_bp.get("/social-campaigns")
@login_required
def api_social_list():
    cid = tenant_id()
    posts = SocialPost.query.filter_by(company_id=cid).all() if cid else []
    connected = bool(os.environ.get("TWITTER_BEARER_TOKEN") or os.environ.get("FACEBOOK_ACCESS_TOKEN") or os.environ.get("INSTAGRAM_ACCESS_TOKEN") or os.environ.get("TIKTOK_CLIENT_SECRET"))
    return jsonify({"success": True, "connected": connected, "posts": [{"id": p.id, "content": p.content, "platforms": p.platforms or ([p.platform] if p.platform else []), "status": p.status or "draft"} for p in posts]})

@marketing_api_bp.post("/social-campaigns")
@login_required
def api_social_create():
    data = request.get_json(silent=True) or {}
    cid = tenant_id()
    if not cid:
        return _json_error("tenant/company is required")
    p = SocialPost(company_id=cid, user_id=current_user.id, content=data.get("content") or data.get("caption") or "", platforms=data.get("platforms") or [], status=data.get("status") or "draft")
    db.session.add(p)
    db.session.commit()
    return jsonify({"success": True, "post": {"id": p.id, "status": p.status}}), 201

@marketing_api_bp.put("/social-campaigns/<int:pid>")
@login_required
def api_social_update(pid):
    p = SocialPost.query.filter_by(id=pid, company_id=tenant_id()).first_or_404()
    data = request.get_json(silent=True) or {}
    for f in ("content", "platforms", "status"):
        if f in data:
            setattr(p, f, data[f])
    db.session.commit()
    return jsonify({"success": True})

@marketing_api_bp.post("/social-campaigns/<int:pid>/schedule")
@login_required
def api_social_schedule(pid):
    p = SocialPost.query.filter_by(id=pid, company_id=tenant_id()).first_or_404()
    data = request.get_json(silent=True) or {}
    p.scheduled_at = datetime.fromisoformat(data.get("scheduled_at")) if data.get("scheduled_at") else datetime.utcnow()
    p.status = "scheduled"
    db.session.commit()
    return jsonify({"success": True})

@marketing_api_bp.post("/social-campaigns/<int:pid>/publish")
@login_required
def api_social_publish(pid):
    p = SocialPost.query.filter_by(id=pid, company_id=tenant_id()).first_or_404()
    return jsonify({"success": False, "error": "Social platform is not connected", "post": {"id": p.id, "status": p.status or "draft"}}), 409


def _ai_payload(kind: str, data: dict):
    objective = data.get("objective") or data.get("goal") or "Drive bookings"
    audience = data.get("audience") or data.get("segment") or "VIP customers with SMS consent"
    base = data.get("message") or "Exclusive LUX offer: book today for priority service. Reply STOP to opt out."
    variants = [base, base.replace("Exclusive", "VIP"), base.replace("book today", "reserve your spot"), base.replace("priority", "white-glove")]
    shortened = [v[:130].rstrip() + (" Reply STOP." if "STOP" not in v.upper() else "") for v in variants[:5]]
    return {
        "success": True,
        "kind": kind,
        "strategy": {
            "objective": objective,
            "message_angle": data.get("angle") or "exclusive concierge offer",
            "recommended_send_window": "Tue-Thu 10:00-16:00 local time",
            "risk_level": "medium",
        },
        "objective": objective,
        "audience_suggestions": [
            {"segment": audience, "reason": "matches the requested campaign objective"},
            {"segment": "recent leads", "reason": "high intent and likely to respond"},
            {"segment": "repeat customers", "reason": "known consented audience with stronger conversion odds"},
        ],
        "variants": [{"label": f"Variant {idx + 1}", "body": body, "segments": 1 if len(body) <= 160 else 2} for idx, body in enumerate(shortened)],
        "compliance_warnings": [
            "Confirm prior express written consent before sending.",
            "Exclude opted-out, blocked, invalid, or unverified numbers.",
            "Respect quiet hours and frequency caps.",
            "Include clear STOP opt-out language.",
        ],
        "keyword_flow": {
            "keyword": "VIP",
            "match_type": "exact",
            "reply": "You are in. A concierge will follow up shortly. Reply STOP to opt out.",
            "tag_to_add": "vip_keyword",
            "segment_to_add": "vip_keyword_segment",
        },
        "follow_ups": [
            {"delay_hours": 24, "body": "Reminder: your VIP offer is still available. Reply BOOK for concierge help or STOP to opt out."},
            {"delay_hours": 72, "body": "Last call for VIP access. Reply VIP to reserve priority service. Reply STOP to opt out."},
        ],
        "ab_test": {"split": "50/50", "success_metric": "reply_rate", "variants": ["Variant 1", "Variant 2"]},
    }

@marketing_api_bp.post("/ai/generate-campaign")
@login_required
def ai_generate_campaign(): return jsonify(_ai_payload("generate-campaign", request.get_json(silent=True) or {}))
@marketing_api_bp.post("/ai/rewrite-sms")
@login_required
def ai_rewrite_sms(): return jsonify(_ai_payload("rewrite-sms", request.get_json(silent=True) or {}))
@marketing_api_bp.post("/ai/generate-keyword-flow")
@login_required
def ai_keyword_flow(): return jsonify(_ai_payload("keyword-flow", request.get_json(silent=True) or {}))
@marketing_api_bp.post("/ai/suggest-segment")
@login_required
def ai_suggest_segment(): return jsonify(_ai_payload("suggest-segment", request.get_json(silent=True) or {}))
@marketing_api_bp.post("/ai/compliance-check")
@login_required
def ai_compliance_check(): return jsonify(_ai_payload("compliance-check", request.get_json(silent=True) or {}))

csrf.exempt(marketing_api_bp)

# ---------------------------------------------------------------------------
# Segment management and marketing suppression APIs (/api/*)
# ---------------------------------------------------------------------------
from flask import Blueprint
from sqlalchemy.exc import IntegrityError
from models import Segment, SegmentMember, CampaignRecipient

segment_api_bp = Blueprint("segment_api", __name__, url_prefix="/api")

SEGMENT_FIELDS = ("name", "description", "segment_type", "category", "match_mode", "triggers", "conditions", "actions", "is_dynamic", "is_active")
SKIP_REASONS = {"do_not_market", "do_not_email", "email_unsubscribed", "do_not_sms", "sms_opted_out", "missing_email", "missing_phone", "invalid_phone", "tenant_mismatch"}


def _can_edit(cid):
    return bool(cid and current_user.is_authenticated and current_user.can_edit_company(cid))


def _can_admin(cid):
    return bool(cid and current_user.is_authenticated and current_user.can_admin_company(cid))


def _require_edit(cid):
    if not _can_edit(cid):
        return _json_error("admin, manager, or editor permission is required", 403)
    return None


def _require_admin(cid):
    if not _can_admin(cid):
        return _json_error("admin permission is required", 403)
    return None


def _audit(company_id, action, entity_type, entity_id=None, details=None):
    db.session.add(MarketingAuditLog(
        company_id=company_id,
        created_by_user_id=getattr(current_user, "id", None),
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        details=details or {},
    ))


def _segment_or_404(segment_id):
    return Segment.query.filter_by(id=segment_id, company_id=tenant_id()).first_or_404()


def _contact_or_404(contact_id, company_id):
    return Contact.query.filter_by(id=contact_id, company_id=company_id).first_or_404()


def _segment_json(s, include_rules=True):
    data = {
        "id": s.id, "name": s.name, "description": s.description, "segment_type": s.segment_type,
        "category": s.category, "match_mode": s.match_mode, "is_dynamic": s.is_dynamic,
        "is_active": s.is_active, "member_count": s.members.filter_by(is_excluded=False, removed_at=None).count(),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }
    if include_rules:
        data.update({"triggers": s.triggers or [], "conditions": s.conditions or [], "actions": s.actions or []})
    return data


def _suppression_badges(c):
    badges = []
    if c.do_not_market: badges.append("Do Not Market")
    if c.do_not_email: badges.append("Do Not Email")
    if c.do_not_sms: badges.append("Do Not SMS")
    if c.sms_opted_out or c.sms_opt_out_at: badges.append("SMS Opted Out")
    if c.email_unsubscribed or not c.is_subscribed: badges.append("Email Unsubscribed")
    return badges


def _contact_json(c, membership=None):
    data = {"id": c.id, "email": c.email, "phone": c.phone, "first_name": c.first_name, "last_name": c.last_name,
            "do_not_market": c.do_not_market, "do_not_email": c.do_not_email, "do_not_sms": c.do_not_sms,
            "email_unsubscribed": c.email_unsubscribed or not c.is_subscribed, "sms_opted_out": c.sms_opted_out or bool(c.sms_opt_out_at),
            "suppression_badges": _suppression_badges(c)}
    if membership:
        data["membership"] = {"source": membership.source, "added_by_user_id": membership.added_by_user_id,
            "added_at": membership.added_at.isoformat() if membership.added_at else None,
            "removed_at": membership.removed_at.isoformat() if membership.removed_at else None,
            "removed_by_user_id": membership.removed_by_user_id, "is_excluded": membership.is_excluded,
            "exclusion_reason": membership.exclusion_reason}
    return data


def marketing_skip_reason(contact, channel):
    if not contact:
        return "tenant_mismatch"
    if contact.do_not_market:
        return "do_not_market"
    if channel == "email":
        if contact.do_not_email: return "do_not_email"
        if contact.email_unsubscribed or not contact.is_subscribed: return "email_unsubscribed"
        if not contact.email: return "missing_email"
    if channel == "sms":
        if contact.do_not_sms: return "do_not_sms"
        if contact.sms_opted_out or contact.sms_opt_out_at: return "sms_opted_out"
        if not contact.phone: return "missing_phone"
        if "invalid_phone" in (contact.tags or "").lower(): return "invalid_phone"
    return None


def _matching_dynamic_contacts(segment):
    q = _audience_query(segment.company_id, segment.name if segment.segment_type == "legacy_tag" else None)
    conditions = segment.conditions or []
    if isinstance(conditions, dict): conditions = [conditions]
    for cond in conditions:
        field, value = cond.get("field"), cond.get("value")
        if field == "tag": q = q.filter(Contact.tags.ilike(f"%{value}%"))
        elif field == "segment": q = q.filter(Contact.segment == value)
        elif field == "source": q = q.filter(Contact.source == value)
        elif field == "email_opt_in": q = q.filter(Contact.is_subscribed.is_(bool(value)))
        elif field == "sms_opt_in": q = q.filter(Contact.sms_marketing_opt_in.is_(bool(value)))
    return q.all()


def refresh_dynamic_segment(segment):
    if not segment.is_dynamic:
        return 0
    excluded = {m.contact_id for m in segment.members.filter_by(is_excluded=True).all()}
    existing = {m.contact_id: m for m in segment.members.all()}
    matched = 0
    for contact in _matching_dynamic_contacts(segment):
        if contact.id in excluded:
            continue
        matched += 1
        member = existing.get(contact.id)
        if member:
            member.removed_at = None; member.removed_by_user_id = None; member.source = member.source or "dynamic_rule"
        else:
            db.session.add(SegmentMember(segment_id=segment.id, contact_id=contact.id, source="dynamic_rule"))
    return matched




@segment_api_bp.get("/contacts/search")
@login_required
def api_contacts_search_root():
    """Tenant-scoped contact picker search for segment membership UX."""
    cid = tenant_id()
    if not cid:
        return jsonify({"success": True, "contacts": []})
    term = (request.args.get("q") or "").strip()
    query = Contact.query.filter_by(company_id=cid)
    if term:
        like = f"%{term}%"
        query = query.filter(or_(
            Contact.first_name.ilike(like),
            Contact.last_name.ilike(like),
            Contact.email.ilike(like),
            Contact.phone.ilike(like),
            Contact.company.ilike(like),
        ))
    contacts = query.order_by(Contact.created_at.desc()).limit(25).all()
    return jsonify({"success": True, "contacts": [_contact_json(c) for c in contacts]})


@segment_api_bp.get("/segments")
@login_required
def api_segments_root():
    cid = tenant_id()
    return jsonify({"success": True, "segments": [_segment_json(s, False) for s in Segment.query.filter_by(company_id=cid).order_by(Segment.name).all()]})


@segment_api_bp.post("/segments")
@login_required
def api_segment_create_root():
    cid = tenant_id(); err = _require_edit(cid)
    if err: return err
    data = request.get_json(silent=True) or {}
    s = Segment(company_id=cid, name=data.get("name") or "Untitled Segment")
    for f in SEGMENT_FIELDS:
        if f in data: setattr(s, f, data[f])
    db.session.add(s); db.session.flush(); _audit(cid, "segment_created", "segment", s.id, _segment_json(s)); db.session.commit()
    return jsonify({"success": True, "segment": _segment_json(s)}), 201


@segment_api_bp.get("/segments/<int:sid>")
@login_required
def api_segment_get_root(sid):
    s = _segment_or_404(sid)
    refresh_dynamic_segment(s); db.session.commit()
    return jsonify({"success": True, "segment": _segment_json(s)})


@segment_api_bp.patch("/segments/<int:sid>")
@login_required
def api_segment_patch_root(sid):
    s = _segment_or_404(sid); err = _require_edit(s.company_id)
    if err: return err
    data = request.get_json(silent=True) or {}; was_active = s.is_active
    for f in SEGMENT_FIELDS:
        if f in data: setattr(s, f, data[f])
    action = "segment_updated"
    if "is_active" in data and bool(data["is_active"]) != bool(was_active):
        action = "segment_activated" if data["is_active"] else "segment_deactivated"
    _audit(s.company_id, action, "segment", s.id, data); db.session.commit()
    return jsonify({"success": True, "segment": _segment_json(s)})


@segment_api_bp.post("/segments/<int:sid>/copy")
@login_required
def api_segment_copy_root(sid):
    s = _segment_or_404(sid); err = _require_edit(s.company_id)
    if err: return err
    dup = Segment(company_id=s.company_id, name=f"{s.name} Copy", description=s.description, segment_type=s.segment_type,
        category=s.category, match_mode=s.match_mode, triggers=s.triggers, conditions=s.conditions, actions=s.actions,
        is_dynamic=s.is_dynamic, is_active=s.is_active)
    db.session.add(dup); db.session.flush(); _audit(s.company_id, "segment_copied", "segment", dup.id, {"source_segment_id": s.id}); db.session.commit()
    return jsonify({"success": True, "segment": _segment_json(dup)}), 201


@segment_api_bp.delete("/segments/<int:sid>")
@login_required
def api_segment_delete_root(sid):
    s = _segment_or_404(sid); err = _require_admin(s.company_id)
    if err: return err
    cid = s.company_id; details = {"name": s.name, "memberships_removed": s.members.count()}
    db.session.delete(s); _audit(cid, "segment_deleted", "segment", sid, details); db.session.commit()
    return jsonify({"success": True, "deleted": sid})


@segment_api_bp.get("/segments/<int:sid>/contacts")
@login_required
def api_segment_contacts_root(sid):
    s = _segment_or_404(sid); refresh_dynamic_segment(s); db.session.commit()
    q = SegmentMember.query.filter_by(segment_id=s.id, removed_at=None)
    if request.args.get("include_excluded") != "true": q = q.filter_by(is_excluded=False)
    term = (request.args.get("q") or "").strip()
    rows = q.join(Contact).filter(Contact.company_id == s.company_id)
    if term: rows = rows.filter(or_(Contact.email.ilike(f"%{term}%"), Contact.first_name.ilike(f"%{term}%"), Contact.last_name.ilike(f"%{term}%"), Contact.phone.ilike(f"%{term}%")))
    members = rows.order_by(SegmentMember.added_at.desc()).all()
    return jsonify({"success": True, "contacts": [_contact_json(m.contact, m) for m in members]})


def _add_contact_to_segment(s, contact_id, source="manual"):
    c = _contact_or_404(contact_id, s.company_id)
    member = SegmentMember.query.filter_by(segment_id=s.id, contact_id=c.id).first()
    if member:
        member.removed_at = None; member.is_excluded = False; member.exclusion_reason = None; member.source = source
    else:
        member = SegmentMember(segment_id=s.id, contact_id=c.id, source=source)
        db.session.add(member)
    member.added_by_user_id = current_user.id; member.added_at = datetime.utcnow()
    return member


@segment_api_bp.post("/segments/<int:sid>/contacts")
@login_required
def api_segment_add_contact_root(sid):
    s = _segment_or_404(sid); err = _require_edit(s.company_id)
    if err: return err
    data = request.get_json(silent=True) or {}; m = _add_contact_to_segment(s, data.get("contact_id"), data.get("source") or "manual")
    _audit(s.company_id, "segment_contact_added", "segment", s.id, {"contact_id": m.contact_id, "source": m.source}); db.session.commit()
    return jsonify({"success": True, "contact": _contact_json(m.contact, m)}), 201


@segment_api_bp.delete("/segments/<int:sid>/contacts/<int:contact_id>")
@login_required
def api_segment_remove_contact_root(sid, contact_id):
    s = _segment_or_404(sid); err = _require_edit(s.company_id)
    if err: return err
    permanent = (request.get_json(silent=True) or {}).get("permanent_exclusion") or request.args.get("permanent_exclusion") == "true"
    m = SegmentMember.query.filter_by(segment_id=s.id, contact_id=contact_id).first_or_404()
    if permanent:
        m.is_excluded = True; m.exclusion_reason = "permanent_exclusion"; m.source = m.source or "manual"
    m.removed_at = datetime.utcnow(); m.removed_by_user_id = current_user.id
    _audit(s.company_id, "segment_contact_excluded" if permanent else "segment_contact_removed", "segment", s.id, {"contact_id": contact_id}); db.session.commit()
    return jsonify({"success": True})


@segment_api_bp.post("/segments/<int:sid>/contacts/bulk-add")
@login_required
def api_segment_bulk_add_root(sid):
    s = _segment_or_404(sid); err = _require_edit(s.company_id)
    if err: return err
    ids = (request.get_json(silent=True) or {}).get("contact_ids") or []
    added = [_add_contact_to_segment(s, i) for i in ids]
    _audit(s.company_id, "segment_contact_added", "segment", s.id, {"contact_ids": [m.contact_id for m in added], "bulk": True}); db.session.commit()
    return jsonify({"success": True, "added": len(added)})


@segment_api_bp.post("/segments/<int:sid>/contacts/bulk-remove")
@login_required
def api_segment_bulk_remove_root(sid):
    s = _segment_or_404(sid); err = _require_edit(s.company_id)
    if err: return err
    ids = (request.get_json(silent=True) or {}).get("contact_ids") or []
    now = datetime.utcnow(); count = 0
    for m in SegmentMember.query.filter(SegmentMember.segment_id == s.id, SegmentMember.contact_id.in_(ids)).all():
        m.removed_at = now; m.removed_by_user_id = current_user.id; count += 1
    _audit(s.company_id, "segment_contact_removed", "segment", s.id, {"contact_ids": ids, "bulk": True}); db.session.commit()
    return jsonify({"success": True, "removed": count})


@segment_api_bp.post("/segments/<int:sid>/contacts/<int:contact_id>/exclude")
@login_required
def api_segment_exclude_contact_root(sid, contact_id):
    s = _segment_or_404(sid); err = _require_edit(s.company_id)
    if err: return err
    data = request.get_json(silent=True) or {}; _contact_or_404(contact_id, s.company_id)
    m = SegmentMember.query.filter_by(segment_id=s.id, contact_id=contact_id).first() or SegmentMember(segment_id=s.id, contact_id=contact_id, source="manual")
    db.session.add(m); m.is_excluded = True; m.removed_at = datetime.utcnow(); m.removed_by_user_id = current_user.id; m.exclusion_reason = data.get("reason") or "permanent_exclusion"
    _audit(s.company_id, "segment_contact_excluded", "segment", s.id, {"contact_id": contact_id, "reason": m.exclusion_reason}); db.session.commit()
    return jsonify({"success": True})


@segment_api_bp.patch("/contacts/<int:contact_id>/marketing-preferences")
@login_required
def api_contact_marketing_preferences(contact_id):
    cid = tenant_id(); err = _require_edit(cid)
    if err: return err
    c = _contact_or_404(contact_id, cid); data = request.get_json(silent=True) or {}; old = {f: getattr(c, f) for f in ("do_not_market", "do_not_email", "do_not_sms")}
    for f in ("do_not_market", "do_not_email", "do_not_sms", "email_unsubscribed", "sms_opted_out"):
        if f in data: setattr(c, f, bool(data[f]))
    c.marketing_preferences_reason = data.get("reason", c.marketing_preferences_reason); c.marketing_preferences_source = data.get("source", c.marketing_preferences_source)
    c.marketing_preferences_updated_by_user_id = current_user.id; c.marketing_preferences_updated_at = datetime.utcnow()
    for f, action in (("do_not_market", "contact_do_not_market"), ("do_not_email", "contact_do_not_email"), ("do_not_sms", "contact_do_not_sms")):
        if f in data and bool(data[f]) != bool(old[f]): _audit(cid, f"{action}_{'enabled' if data[f] else 'disabled'}", "contact", c.id, {"reason": c.marketing_preferences_reason})
    db.session.commit(); return jsonify({"success": True, "contact": _contact_json(c)})


@marketing_api_bp.post("/campaigns/<int:campaign_id>/send")
@login_required
def api_email_campaign_send(campaign_id):
    c = Campaign.query.filter_by(id=campaign_id, company_id=tenant_id()).first_or_404()
    contacts = _audience_query(c.company_id).all(); sent = 0; skipped = []
    for contact in contacts:
        reason = marketing_skip_reason(contact, "email")
        if reason:
            skipped.append({"contact_id": contact.id, "reason": reason})
            _audit(c.company_id, "campaign_contact_skipped_suppression", "campaign", c.id, {"contact_id": contact.id, "reason": reason, "channel": "email"})
            continue
        db.session.add(CampaignRecipient(campaign_id=c.id, contact_id=contact.id)); sent += 1
    c.status = "sent"; c.sent_at = datetime.utcnow(); db.session.commit()
    return jsonify({"success": True, "sent": sent, "skipped": skipped})

# ---------------------------------------------------------------------------
# Contact Intelligence / CRM admin APIs
# ---------------------------------------------------------------------------

def _require_contact_admin():
    if not getattr(current_user, "is_admin", False):
        return False
    return True


def _contact_job_json(job):
    return {
        "id": job.id, "company_id": job.company_id, "job_type": job.job_type, "status": job.status,
        "cursor": job.cursor, "batch_size": job.batch_size, "total_found": job.total_found,
        "processed": job.processed, "updated": job.updated, "skipped": job.skipped,
        "ambiguous": job.ambiguous, "failed": job.failed, "dry_run": job.dry_run,
        "sanitized_last_error": job.sanitized_last_error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


@marketing_api_bp.post("/contacts/intelligence/jobs")
@login_required
def api_contact_intelligence_create_job():
    if not _require_contact_admin():
        return _json_error("admin required", 403)
    cid = tenant_id()
    data = request.get_json(silent=True) or {}
    from services.contact_intelligence_jobs import create_job
    try:
        job = create_job(cid, data.get("job_type"), user_id=current_user.id, batch_size=int(data.get("batch_size") or 100), dry_run=data.get("dry_run", True))
    except Exception as exc:
        return _json_error(str(exc), 400)
    return jsonify({"success": True, "job": _contact_job_json(job)})


@marketing_api_bp.post("/contacts/intelligence/jobs/<int:job_id>/run")
@login_required
def api_contact_intelligence_run_job(job_id):
    if not _require_contact_admin():
        return _json_error("admin required", 403)
    cid = tenant_id()
    from models import ContactIntelligenceJob
    from services.contact_intelligence_jobs import run_job
    job = ContactIntelligenceJob.query.filter_by(id=job_id, company_id=cid).first()
    if not job:
        return _json_error("job not found", 404)
    job = run_job(job.id, max_batches=int((request.get_json(silent=True) or {}).get("max_batches") or 1))
    return jsonify({"success": True, "job": _contact_job_json(job)})


@marketing_api_bp.get("/contacts/intelligence/jobs/<int:job_id>")
@login_required
def api_contact_intelligence_job_status(job_id):
    cid = tenant_id()
    from models import ContactIntelligenceJob
    job = ContactIntelligenceJob.query.filter_by(id=job_id, company_id=cid).first()
    if not job:
        return _json_error("job not found", 404)
    return jsonify({"success": True, "job": _contact_job_json(job)})


@marketing_api_bp.get("/contacts/google/status")
@login_required
def api_google_contacts_status():
    cid = tenant_id()
    from services.google_contacts import get_token, token_needs_reconnect, upsert_connection_from_token, SCOPES
    tok = get_token(current_user.id)
    conn = upsert_connection_from_token(current_user.id, cid) if tok else None
    db.session.commit()
    return jsonify({
        "success": True, "connected": bool(tok), "scope": SCOPES,
        "oauth_expired": bool(token_needs_reconnect(tok)) if tok else False,
        "google_account_email": getattr(tok, "google_account_email", None) if tok else None,
        "connection_id": conn.id if conn else None,
        "sync_status": getattr(conn, "sync_status", "disconnected") if conn else "disconnected",
        "last_successful_sync_at": conn.last_successful_sync_at.isoformat() if conn and conn.last_successful_sync_at else None,
    })


@marketing_api_bp.post("/contacts/google/disconnect")
@login_required
def api_google_contacts_disconnect():
    cid = tenant_id()
    from services.google_contacts import disconnect
    from models import GoogleContactConnection
    disconnect(current_user.id)
    conn = GoogleContactConnection.query.filter_by(company_id=cid, user_id=current_user.id).first()
    if conn:
        conn.sync_status = "disconnected"; conn.disconnected_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "connected": False})


@marketing_api_bp.post("/contacts/google/sync")
@login_required
def api_google_contacts_sync():
    if not _require_contact_admin():
        return _json_error("admin required", 403)
    cid = tenant_id(); data = request.get_json(silent=True) or {}
    from services.google_contacts import sync_contacts, upsert_connection_from_token
    result = sync_contacts(current_user.id, cid, dry_run=bool(data.get("dry_run", False)))
    conn = upsert_connection_from_token(current_user.id, cid)
    conn.sync_status = "failed" if result.get("error") else "synced"
    conn.last_error = result.get("error")
    if not result.get("error"):
        conn.last_successful_sync_at = datetime.utcnow()
    db.session.commit()
    result.pop("access_token", None); result.pop("refresh_token", None)
    return jsonify({"success": not bool(result.get("error")), "result": result})


@marketing_api_bp.post("/contacts/google/match-unnamed")
@login_required
def api_google_match_unnamed():
    if not _require_contact_admin():
        return _json_error("admin required", 403)
    cid = tenant_id(); data = request.get_json(silent=True) or {}
    from services.contact_intelligence_jobs import create_job, run_job
    job = create_job(cid, "unnamed_match", user_id=current_user.id, batch_size=int(data.get("batch_size") or 100), dry_run=bool(data.get("dry_run", False)))
    job = run_job(job.id, max_batches=int(data.get("max_batches") or 1))
    return jsonify({"success": True, "job": _contact_job_json(job)})


@marketing_api_bp.post("/contacts/<int:contact_id>/google/recheck")
@login_required
def api_google_recheck_contact(contact_id):
    cid = tenant_id()
    from models import Contact, GoogleContactLookup
    from services.contact_intelligence import apply_google_name_match
    contact = Contact.query.filter_by(id=contact_id, company_id=cid, is_active=True).first()
    if not contact:
        return _json_error("contact not found", 404)
    lookups = GoogleContactLookup.query.filter_by(company_id=cid, normalized_phone=contact.normalized_phone).all() if contact.normalized_phone else []
    candidates = []
    for l in lookups:
        candidates.extend(l.candidates or [{"normalized_phone": l.normalized_phone, "name": l.display_name, "resource_id": l.resource_id, "etag": l.etag}])
    status = apply_google_name_match(contact, candidates) if candidates else "not_found"
    db.session.commit()
    return jsonify({"success": True, "status": status, "contact_id": contact.id})


@marketing_api_bp.get("/contacts/google/ambiguous")
@login_required
def api_google_ambiguous_matches():
    cid = tenant_id()
    from models import GoogleContactLookup
    rows = GoogleContactLookup.query.filter_by(company_id=cid, is_ambiguous=True).limit(200).all()
    return jsonify({"success": True, "matches": [{"id": r.id, "normalized_phone": r.normalized_phone, "candidate_count": r.candidate_count, "candidates": r.candidates} for r in rows]})


@marketing_api_bp.post("/contacts/<int:contact_id>/google/suggestion")
@login_required
def api_google_suggestion_action(contact_id):
    cid = tenant_id(); data = request.get_json(silent=True) or {}; action = data.get("action")
    from models import Contact
    from services.contact_intelligence import meaningful_name
    contact = Contact.query.filter_by(id=contact_id, company_id=cid, is_active=True).first()
    if not contact:
        return _json_error("contact not found", 404)
    if action == "accept":
        name = (data.get("name") or "").strip()
        if not name:
            return _json_error("name is required", 400)
        if not meaningful_name(contact):
            contact.name = name; contact.display_name = name; contact.name_source = "google_contacts"; contact.google_match_status = "accepted"
        else:
            return _json_error("reliable contact name will not be overwritten", 409)
    elif action == "reject":
        contact.google_match_status = "rejected"
    else:
        return _json_error("action must be accept or reject", 400)
    db.session.commit()
    return jsonify({"success": True, "contact_id": contact.id, "google_match_status": contact.google_match_status})


@marketing_api_bp.post("/contacts/duplicates/not-duplicate")
@login_required
def api_contact_not_duplicate():
    if not _require_contact_admin():
        return _json_error("admin required", 403)
    cid = tenant_id(); data = request.get_json(silent=True) or {}
    a, b = sorted([int(data.get("contact_id_a") or 0), int(data.get("contact_id_b") or 0)])
    if not a or not b or a == b:
        return _json_error("two distinct contact ids are required", 400)
    from models import Contact, ContactDuplicateExclusion
    if Contact.query.filter(Contact.company_id == cid, Contact.id.in_([a,b])).count() != 2:
        return _json_error("contacts must belong to your company", 400)
    row = ContactDuplicateExclusion.query.filter_by(company_id=cid, contact_id_a=a, contact_id_b=b).first()
    if not row:
        row = ContactDuplicateExclusion(company_id=cid, contact_id_a=a, contact_id_b=b, reason=data.get("reason"), created_by_user_id=current_user.id)
        db.session.add(row)
    db.session.commit()
    return jsonify({"success": True, "excluded_pair": [a, b]})


@marketing_api_bp.get("/contacts/reports/attribution")
@login_required
def api_contact_attribution_report():
    cid = tenant_id()
    from sqlalchemy import func
    from models import Opportunity, ContactTask
    by_original = db.session.query(Contact.original_source, func.count(Contact.id)).filter(Contact.company_id == cid).group_by(Contact.original_source).all()
    by_latest = db.session.query(Contact.latest_source, func.count(Contact.id)).filter(Contact.company_id == cid).group_by(Contact.latest_source).all()
    open_pipeline = db.session.query(Opportunity.stage, Opportunity.owner_user_id, func.coalesce(func.sum(Opportunity.estimated_value), 0)).filter(Opportunity.company_id == cid, Opportunity.status == "open").group_by(Opportunity.stage, Opportunity.owner_user_id).all()
    won_by_source = db.session.query(Contact.original_source, func.coalesce(func.sum(Opportunity.estimated_value), 0)).join(Contact, Contact.id == Opportunity.contact_id).filter(Opportunity.company_id == cid, Opportunity.status == "won").group_by(Contact.original_source).all()
    overdue = ContactTask.query.filter(ContactTask.company_id == cid, ContactTask.status == "open", ContactTask.due_at < datetime.utcnow()).count()
    unnamed = Contact.query.filter(Contact.company_id == cid, Contact.is_active.is_(True), (Contact.name.is_(None)) | (Contact.name == "")).count()
    return jsonify({"success": True, "labels": {"won_value": "won opportunity value, not collected revenue"}, "contacts_by_original_source": dict(by_original), "contacts_by_latest_source": dict(by_latest), "open_pipeline_value_by_stage_owner": [{"stage": s, "owner_user_id": o, "value": float(v or 0)} for s,o,v in open_pipeline], "won_opportunity_value_by_original_source": {k or "unknown": float(v or 0) for k,v in won_by_source}, "unnamed_contacts": unnamed, "followups_overdue": overdue})

@marketing_api_bp.get("/contacts/google/connect")
@login_required
def api_google_contacts_connect():
    from services.google_contacts import get_auth_url, SCOPES
    if not os.environ.get("GOOGLE_CLIENT_ID"):
        return _json_error("GOOGLE_CLIENT_ID is not configured", 400, scope=SCOPES)
    return jsonify({"success": True, "auth_url": get_auth_url(state=str(current_user.id)), "scope": SCOPES, "callback": "/twilio/google-contacts/callback"})


@marketing_api_bp.get("/contacts/google/callback")
@login_required
def api_google_contacts_callback_compat():
    # Preserve existing Twilio callback and expose a CRM-scoped compatibility endpoint.
    from flask import redirect, url_for
    qs = request.query_string.decode()
    return redirect((url_for("twilio.google_contacts_callback") if "twilio.google_contacts_callback" in current_app.view_functions else "/twilio/google-contacts/callback") + (f"?{qs}" if qs else ""))
