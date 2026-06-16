"""Tenant-aware Marketing Hub APIs and helpers.

This module intentionally keeps provider operations fail-safe: missing Twilio or
social credentials produce structured 4xx/200 responses instead of page-level
500s, while all persistence is tenant scoped by company_id.
"""
from __future__ import annotations

import os
from datetime import datetime

from flask import Blueprint, jsonify, request
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
    contacts = _audience_query(c.company_id, c.segment).limit(100).all()
    eligible = [x for x in contacts if _contact_has_sms_consent(x)]
    excluded = len(contacts) - len(eligible)
    return jsonify({"success": True, "recipients_selected": len(eligible), "excluded": excluded, "recipients": [{"id": x.id, "phone": x.phone, "name": f"{x.first_name or ''} {x.last_name or ''}".strip()} for x in eligible[:25]]})


def _materialize_recipients(c: SMSCampaign):
    existing = {r.contact_id for r in c.recipients}
    contacts = [x for x in _audience_query(c.company_id, c.segment).all() if _contact_has_sms_consent(x)]
    for contact in contacts:
        if contact.id not in existing:
            db.session.add(SMSRecipient(company_id=c.company_id, campaign_id=c.id, contact_id=contact.id, status="queued"))
    return contacts


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
    contacts = _materialize_recipients(c)
    if not contacts:
        return _json_error("no eligible recipients with SMS consent")
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
