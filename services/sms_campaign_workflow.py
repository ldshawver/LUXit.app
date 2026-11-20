"""Shared SMS campaign create / edit / schedule rules.

Used by both the JSON API (marketing_api.py) and the server-rendered pages
(routes.py: /sms/create, /sms/campaign/<id>/edit) so the two surfaces cannot
drift apart again. Every rule fails closed:

* audience = tenant-owned marketing Segment ids (canonical_tag_ids); free text
  that matches no segment is rejected, never auto-created, never "everyone";
* sender = an authorized tenant phone line the acting user may use;
* scheduling needs message + audience + sender + >= 1 eligible recipient,
  and a future time;
* nothing is editable once any recipient has been attempted;
* choosing a non-promotional purpose (which does not need a marketing opt-in)
  is restricted to owners/admins.
"""
from __future__ import annotations

from datetime import datetime, timezone

from extensions import db
from models import MarketingAuditLog, Segment, SMSCampaign

EDITABLE_STATUSES = {"draft", "scheduled", "paused"}
SCHEDULABLE_STATUSES = {"draft", "paused", "scheduled"}
MAX_MEDIA_URLS = 10
SUMMARY_KEYS = (
    "matching_contacts", "contacts_with_phone", "unique_phone_numbers", "eligible_recipients",
    "missing_phone_numbers", "invalid_phone_numbers", "duplicate_phone_numbers", "opted_out_contacts",
    "missing_sms_consent", "archived_or_suppressed", "campaign_purpose", "explanation",
)


class CampaignWorkflowError(Exception):
    def __init__(self, message, status=400, code="invalid_campaign", counts=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.counts = counts


def audience_summary(counts):
    return {k: (counts or {}).get(k) for k in SUMMARY_KEYS}


def can_edit(user, company_id) -> bool:
    return bool(company_id and user is not None and user.is_authenticated and user.can_edit_company(company_id))


def can_admin(user, company_id) -> bool:
    return bool(company_id and user is not None and user.is_authenticated and user.can_admin_company(company_id))


def require_edit(user, company_id):
    if not can_edit(user, company_id):
        raise CampaignWorkflowError(
            "Owner, admin, manager, or editor permission is required to change SMS campaigns.", 403, "role_required")


def require_admin(user, company_id):
    if not can_admin(user, company_id):
        raise CampaignWorkflowError(
            "Only workspace owners and admins can schedule or send SMS campaigns.", 403, "role_required")


def is_locked(campaign) -> bool:
    from services.contact_audience import recipients_already_attempted
    return (campaign.status or "draft") not in EDITABLE_STATUSES or (
        campaign.id is not None and recipients_already_attempted(campaign))


def parse_media_urls(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.replace("\r", "").split("\n")
    urls = [str(u).strip() for u in (raw or []) if str(u).strip()]
    if len(urls) > MAX_MEDIA_URLS:
        raise CampaignWorkflowError(f"At most {MAX_MEDIA_URLS} media URLs are allowed.")
    for u in urls:
        if not u.lower().startswith("https://") or any(ch.isspace() for ch in u):
            raise CampaignWorkflowError("Media URLs must be public https:// links.")
    return urls


def parse_schedule(when):
    if when in (None, ""):
        return None
    if isinstance(when, datetime):
        value = when
    else:
        try:
            value = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        except ValueError:
            raise CampaignWorkflowError("scheduled_at must be an ISO-8601 date/time.", 400, "invalid_schedule")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def apply_audience(campaign, data, company_id, user) -> bool:
    """Apply segment/purpose fields from ``data``; returns True when changed."""
    from services.contact_audience import canonical_tag_ids
    from services.sms_consent_purpose import CAMPAIGN_PURPOSES
    changed = False
    audience = dict(campaign.audience_filter or {})
    if any(k in data for k in ("selected_tag_ids", "segment", "segment_id", "segment_ids")):
        raw_ids = data.get("selected_tag_ids") or data.get("segment_ids") or (
            [data["segment_id"]] if data.get("segment_id") else [])
        if isinstance(raw_ids, (str, int)):
            raw_ids = [raw_ids]
        names = [n.strip() for n in str(data.get("segment") or "").split(",") if n.strip()] if not raw_ids else []
        from services.contact_audience import AudienceError
        try:
            tag_ids = canonical_tag_ids(company_id, tag_ids=raw_ids, tag_names=names)
        except AudienceError as exc:
            # foreign, system-managed or ambiguous audience -- never guessed at
            raise CampaignWorkflowError(str(exc), 400, "audience_invalid")
        if (raw_ids or names) and not tag_ids:
            raise CampaignWorkflowError(
                "That segment was not found in this workspace. Choose an existing segment.", 400, "audience_invalid")
        if not tag_ids:
            raise CampaignWorkflowError("Choose a segment for this campaign's audience.", 400, "audience_required")
        segs = Segment.query.filter(Segment.company_id == company_id, Segment.id.in_(tag_ids)).order_by(Segment.name).all()
        if sorted(tag_ids) != sorted(audience.get("selected_tag_ids") or campaign.selected_tag_ids or []):
            changed = True
        campaign.selected_tag_ids = sorted(tag_ids)
        campaign.segment = ", ".join(s.name for s in segs)[:100]
        audience["selected_tag_ids"] = sorted(tag_ids)
    if "campaign_purpose" in data and data.get("campaign_purpose") not in (None, ""):
        purpose = str(data.get("campaign_purpose")).strip().lower()
        if purpose not in CAMPAIGN_PURPOSES:
            raise CampaignWorkflowError(f"campaign_purpose must be one of {', '.join(CAMPAIGN_PURPOSES)}.")
        current = audience.get("campaign_purpose") or "promotional"
        if purpose != current and purpose != "promotional" and not can_admin(user, company_id):
            raise CampaignWorkflowError(
                "Only owners and admins can mark a campaign as conversational or transactional "
                "(those purposes do not require a marketing opt-in).", 403, "role_required")
        if (audience.get("campaign_purpose") or "promotional") != purpose:
            changed = True
        audience["campaign_purpose"] = purpose
    campaign.audience_filter = audience
    return changed


def apply_sender(campaign, raw_sender_id, company_id, user):
    from services.phone_line_service import PhoneLineService
    if raw_sender_id in (None, ""):
        campaign.from_phone_number_id = None
        campaign.from_phone_number = None
        return
    try:
        sender_id = int(raw_sender_id)
    except (TypeError, ValueError):
        raise CampaignWorkflowError("Choose a sender phone line.", 400, "sender_invalid")
    sender = PhoneLineService.resolve_campaign_sender(company_id, sender_id, user=user)
    if not sender.get("success"):
        raise CampaignWorkflowError(sender["error"], 403, "sender_not_authorized")
    campaign.from_phone_number_id = sender["phone_number"].id
    campaign.from_phone_number = sender["from_phone"]


def resolve_sender_for_schedule(campaign, user):
    from services.phone_line_service import PhoneLineService
    sender = PhoneLineService.resolve_campaign_sender(campaign.company_id, campaign.from_phone_number_id, user=user)
    if not sender.get("success"):
        raise CampaignWorkflowError(
            sender.get("error") or "Choose an authorized sender phone line before scheduling.", 409
            if not campaign.from_phone_number_id else 403, "sender_required")
    campaign.from_phone_number_id = sender["phone_number"].id
    campaign.from_phone_number = sender["from_phone"]


def rebuild_pending(campaign, user=None):
    """Re-materialize pending recipients (idempotent: the pending set is
    replaced, never appended to) and audit every exclusion by reason, with
    contact ids only. Runs inside the caller's transaction."""
    from services.contact_audience import RecipientsLocked, resolve_sms_campaign_recipients
    try:
        resolved = resolve_sms_campaign_recipients(campaign, materialize=True)
    except RecipientsLocked as exc:
        raise CampaignWorkflowError(str(exc), 409, "campaign_locked")
    counts = resolved["counts"]
    by_reason = {}
    for contact_id, reason in resolved.get("skipped") or []:
        by_reason.setdefault(reason, []).append(contact_id)
    for reason, ids in sorted(by_reason.items()):
        _audit(campaign.company_id, user, "campaign_contact_skipped_suppression", campaign.id,
               {"reason": reason, "count": len(ids), "contact_ids": sorted(ids)[:1000]})
    campaign.recipient_resolution = counts
    campaign.scheduled_preview_count = counts["matching_contacts"]
    campaign.scheduled_unique_phone_count = counts["unique_phone_numbers"]
    campaign.scheduled_eligible_recipient_count = counts["eligible_recipients"]
    return counts


def has_audience(campaign) -> bool:
    return bool(campaign.selected_tag_ids or (campaign.audience_filter or {}).get("selected_tag_ids") or campaign.segment)


def _audit(company_id, user, action, campaign_id, details):
    db.session.add(MarketingAuditLog(company_id=company_id, created_by_user_id=getattr(user, "id", None),
                                     entity_type="sms_campaign", entity_id=campaign_id, action=action,
                                     details=details or {}))


def _event(name, campaign, user, **props):
    from services.product_events import record
    record(name, company_id=campaign.company_id, user_id=getattr(user, "id", None), entity_type="sms_campaign",
           entity_id=campaign.id, properties=props)


def create_campaign(company_id, user, data, *, ensure_opt_out):
    """Create a draft. Scheduling is a separate, admin-only step."""
    require_edit(user, company_id)
    message = str(data.get("message") or "")
    campaign = SMSCampaign(company_id=company_id, created_by_user_id=user.id,
                           name=data.get("name") or "Untitled SMS Campaign", objective=data.get("objective"),
                           message=ensure_opt_out(message), status="draft", selected_tag_ids=[], audience_filter={})
    if data.get("from_phone_number_id") not in (None, ""):
        apply_sender(campaign, data.get("from_phone_number_id"), company_id, user)
    if any(data.get(k) for k in ("selected_tag_ids", "segment", "segment_id", "segment_ids")) or data.get("campaign_purpose"):
        apply_audience(campaign, data, company_id, user)
    media = parse_media_urls(data.get("media_urls"))
    if media is not None:
        campaign.media_urls = media
    for field in ("batch_size", "send_rate_per_minute"):
        if data.get(field) not in (None, ""):
            try:
                setattr(campaign, field, max(1, int(data.get(field))))
            except (TypeError, ValueError):
                raise CampaignWorkflowError(f"{field} must be a whole number.")
    db.session.add(campaign)
    db.session.flush()
    _audit(company_id, user, "sms_campaign_created", campaign.id, {"has_audience": has_audience(campaign)})
    _event("sms_campaign_draft_created", campaign, user, has_audience=has_audience(campaign),
           has_sender=bool(campaign.from_phone_number_id))
    return campaign


def update_campaign(campaign, user, data, *, ensure_opt_out):
    """Edit a draft/scheduled/paused campaign before sending begins.

    Audience/purpose changes rebuild pending recipients; on a scheduled
    campaign the edit is refused (caller must roll back) if the new audience
    has no eligible recipient. Returns the new counts when rebuilt, else None.
    """
    company_id = campaign.company_id
    require_edit(user, company_id)
    if is_locked(campaign):
        raise CampaignWorkflowError("This campaign has started sending (or finished) and can no longer be edited.",
                                    409, "campaign_locked")
    if "scheduled_at" in data and data.get("scheduled_at") not in (None, ""):
        require_admin(user, company_id)
    for field in ("name", "objective"):
        if field in data and data[field] is not None:
            setattr(campaign, field, data[field])
    if "message" in data:
        if not str(data["message"] or "").strip():
            raise CampaignWorkflowError("Message body cannot be empty.")
        campaign.message = ensure_opt_out(data["message"])
    if "from_phone_number_id" in data:
        apply_sender(campaign, data.get("from_phone_number_id"), company_id, user)
    audience_changed = apply_audience(campaign, data, company_id, user)
    if "media_urls" in data:
        campaign.media_urls = parse_media_urls(data.get("media_urls")) or []
    for field in ("batch_size", "send_rate_per_minute"):
        if data.get(field) not in (None, ""):
            try:
                setattr(campaign, field, max(1, int(data.get(field))))
            except (TypeError, ValueError):
                raise CampaignWorkflowError(f"{field} must be a whole number.")
    if "scheduled_at" in data:
        when = parse_schedule(data.get("scheduled_at"))
        if when is None and campaign.status == "scheduled":
            raise CampaignWorkflowError("Use status 'draft' to unschedule a campaign.", 400, "invalid_schedule")
        if when is not None and when <= datetime.utcnow():
            raise CampaignWorkflowError("The scheduled time must be in the future.", 400, "invalid_schedule")
        campaign.scheduled_at = when
        if data.get("timezone"):
            campaign.scheduled_timezone = str(data["timezone"])[:80]
    status = data.get("status")
    if status == "draft":
        campaign.status = "draft"
    elif status not in (None, "", campaign.status):
        raise CampaignWorkflowError(
            "Status can only be changed back to draft here; use schedule, send, pause or cancel.", 400,
            "invalid_status_change")
    counts = None
    if campaign.status == "scheduled":
        resolve_sender_for_schedule(campaign, user)
        if audience_changed or "campaign_purpose" in data or campaign.recipients.count() == 0:
            counts = rebuild_pending(campaign, user)
            if not counts["eligible_recipients"]:
                raise CampaignWorkflowError(counts["explanation"], 400, "no_eligible_recipients", counts)
    elif audience_changed and campaign.recipients.count():
        counts = rebuild_pending(campaign, user)
    if audience_changed:
        _event("sms_campaign_audience_changed", campaign, user, status=campaign.status,
               rebuilt=counts is not None, eligible=(counts or {}).get("eligible_recipients"))
    _audit(company_id, user, "sms_campaign_updated", campaign.id,
           {"fields": sorted(k for k in data.keys() if k not in ("message", "csrf_token")),
            "message_changed": "message" in data, "recipients_rebuilt": counts is not None})
    return counts


def schedule_campaign(campaign, user, when=None, timezone_name=None, sender_id=None):
    """Fail-closed scheduling. Caller commits on success, rolls back on error."""
    require_admin(user, campaign.company_id)
    from services.contact_audience import recipients_already_attempted
    if (campaign.status or "draft") not in SCHEDULABLE_STATUSES or recipients_already_attempted(campaign):
        raise CampaignWorkflowError("Only a campaign that has not started sending can be scheduled.", 409,
                                    "campaign_locked")
    if not str(campaign.message or "").strip():
        raise CampaignWorkflowError("Add a message before scheduling.", 400, "invalid_campaign")
    if not has_audience(campaign):
        raise CampaignWorkflowError("Choose a segment before scheduling.", 400, "audience_required")
    when = parse_schedule(when)
    if when is not None and when <= datetime.utcnow():
        raise CampaignWorkflowError("The scheduled time must be in the future.", 400, "invalid_schedule")
    if sender_id not in (None, ""):
        apply_sender(campaign, sender_id, campaign.company_id, user)
    resolve_sender_for_schedule(campaign, user)
    require_provider(campaign.company_id)
    campaign.scheduled_at = when or datetime.utcnow()
    campaign.scheduled_timezone = timezone_name or campaign.scheduled_timezone or "UTC"
    campaign.scheduled_by_user_id = user.id
    counts = rebuild_pending(campaign, user)
    if not counts["eligible_recipients"]:
        raise CampaignWorkflowError(counts["explanation"], 400, "no_eligible_recipients", counts)
    campaign.status = "scheduled"
    _audit(campaign.company_id, user, "sms_campaign_scheduled", campaign.id,
           {"scheduled_at": campaign.scheduled_at.isoformat(), "eligible": counts["eligible_recipients"],
            "from_phone_number_id": campaign.from_phone_number_id})
    _event("sms_campaign_scheduled", campaign, user, eligible=counts["eligible_recipients"],
           matching=counts["matching_contacts"], from_phone_number_id=campaign.from_phone_number_id)
    return counts


def prepare_send_now(campaign, user, sender_id=None):
    """Checks + fresh recipient build for an immediate send. Returns
    ``(resume, counts)``; ``resume`` is True for a paused, partially sent
    campaign (nothing is rebuilt). Caller commits, then calls
    SMSService.send_campaign / queue_campaign_send."""
    from services.contact_audience import recipients_already_attempted
    from services.sms_service import SMSService
    require_admin(user, campaign.company_id)
    if campaign.status == "paused" and recipients_already_attempted(campaign):
        resolve_sender_for_schedule(campaign, user)
        require_provider(campaign.company_id)
        return True, None
    if campaign.status in SMSService.ACTIVE_STATUSES or campaign.status in (SMSService.BLOCKED_SEND_STATUSES - {"scheduled"}) \
            or recipients_already_attempted(campaign):
        raise CampaignWorkflowError("campaign has no unsent recipients; duplicate send prevented", 409, "duplicate_send")
    if not str(campaign.message or "").strip():
        raise CampaignWorkflowError("message body is empty")
    if not has_audience(campaign):
        raise CampaignWorkflowError("Choose a segment before sending.", 400, "audience_required")
    if sender_id not in (None, ""):
        apply_sender(campaign, sender_id, campaign.company_id, user)
    resolve_sender_for_schedule(campaign, user)
    require_provider(campaign.company_id)
    counts = rebuild_pending(campaign, user)
    if not counts["eligible_recipients"]:
        raise CampaignWorkflowError(counts["explanation"], 400, "no_eligible_recipients", counts)
    if campaign.status == "scheduled":
        campaign.status = "draft"  # explicit "send now"; begin_send performs the guarded transition
    return False, counts


def provider_ready(company_id):
    """Config-only check (no network): can this workspace send SMS at all?
    Tenant Twilio account, or -- only for the platform's own workspaces --
    the platform Twilio environment. Mirrors SMSService.send_sms (T-16)."""
    import os
    from models import TwilioAccount
    from services.license_service import uses_platform_sender
    account = TwilioAccount.query.filter_by(company_id=company_id, is_active=True).first()
    if account and account.is_configured:
        return True, None
    if uses_platform_sender(company_id):
        missing = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN") if not os.environ.get(k)]
        if not missing:
            return True, None
        return False, "The platform Twilio account is not configured (" + ", ".join(missing) + ")."
    return False, "Twilio is not connected for this workspace. Connect a Twilio account in Phone settings before sending."


def require_provider(company_id):
    ok, reason = provider_ready(company_id)
    if not ok:
        raise CampaignWorkflowError(reason, 409, "provider_not_configured")
