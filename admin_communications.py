"""Admin-facing communications reconciliation views.

These routes are intentionally additive: they summarize the existing PWA
phone/SMS/Twilio implementation and link back to the backend-connected
settings, campaign, call-log, voicemail, number-management, and integration
surfaces without replacing the PWA phone routes or webhook logic.
"""

from flask import Blueprint, abort, render_template, request
from flask_login import current_user, login_required

from extensions import db

admin_communications_bp = Blueprint("admin_communications", __name__)


def _is_admin_user() -> bool:
    return bool(
        getattr(current_user, "is_admin", False)
        or getattr(current_user, "is_platform_admin", False)
    )


def _default_company():
    getter = getattr(current_user, "get_default_company", None)
    return getter() if callable(getter) else None


@admin_communications_bp.route("/admin/communications")
@login_required
def communications_admin():
    """SMS & Calls admin page backed by existing PWA phone/Twilio data."""
    if not _is_admin_user():
        abort(403)

    from models import (
        AutoReplyRule,
        PhoneSettings,
        SMSCampaign,
        TwilioAccount,
        TwilioCallLog,
        TwilioPhoneNumber,
        VoiceVoicemailMessage,
    )

    company = _default_company()
    if not company:
        abort(404)

    status_filter = (request.args.get("status") or "all").strip().lower()
    direction_filter = (request.args.get("direction") or "all").strip().lower()
    search = (request.args.get("q") or "").strip()

    call_query = TwilioCallLog.query.filter_by(company_id=company.id)
    if status_filter != "all":
        if status_filter == "recordings":
            call_query = call_query.filter(TwilioCallLog.recording_url.isnot(None))
        elif status_filter == "voicemail":
            call_query = call_query.filter(
                db.or_(TwilioCallLog.status == "voicemail", TwilioCallLog.voicemail_url.isnot(None))
            )
        else:
            call_query = call_query.filter(TwilioCallLog.status == status_filter)
    if direction_filter in {"inbound", "outbound"}:
        call_query = call_query.filter(TwilioCallLog.direction == direction_filter)
    if search:
        like = f"%{search}%"
        call_query = call_query.filter(
            db.or_(
                TwilioCallLog.from_number.ilike(like),
                TwilioCallLog.to_number.ilike(like),
                TwilioCallLog.caller_name.ilike(like),
                TwilioCallLog.twilio_sid.ilike(like),
            )
        )

    calls = call_query.order_by(TwilioCallLog.created_at.desc()).limit(75).all()

    phone_settings = PhoneSettings.query.filter_by(company_id=company.id).first()
    twilio_account = TwilioAccount.query.filter_by(company_id=company.id).first()
    auto_reply_rules = AutoReplyRule.query.filter_by(company_id=company.id).order_by(
        AutoReplyRule.priority.desc(), AutoReplyRule.created_at.desc()
    ).limit(20).all()
    phone_numbers = TwilioPhoneNumber.query.filter_by(company_id=company.id).order_by(
        TwilioPhoneNumber.is_primary.desc(), TwilioPhoneNumber.created_at.desc()
    ).all()
    voicemails = VoiceVoicemailMessage.query.filter_by(
        company_id=company.id, is_deleted=False
    ).order_by(VoiceVoicemailMessage.created_at.desc()).limit(30).all()
    sms_campaigns = SMSCampaign.query.order_by(SMSCampaign.created_at.desc()).limit(8).all()

    call_counts = {
        "all": TwilioCallLog.query.filter_by(company_id=company.id).count(),
        "missed": TwilioCallLog.query.filter_by(company_id=company.id, status="missed").count(),
        "voicemail": TwilioCallLog.query.filter_by(company_id=company.id, status="voicemail").count(),
        "forwarded": TwilioCallLog.query.filter_by(company_id=company.id, status="forwarded").count(),
        "recordings": TwilioCallLog.query.filter(
            TwilioCallLog.company_id == company.id,
            TwilioCallLog.recording_url.isnot(None),
        ).count(),
    }

    return render_template(
        "admin/communications.html",
        company=company,
        phone_settings=phone_settings,
        twilio_account=twilio_account,
        auto_reply_rules=auto_reply_rules,
        sms_campaigns=sms_campaigns,
        phone_numbers=phone_numbers,
        calls=calls,
        call_counts=call_counts,
        voicemails=voicemails,
        status_filter=status_filter,
        direction_filter=direction_filter,
        search=search,
    )
