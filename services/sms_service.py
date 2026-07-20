"""SMS Service for SMS campaign management with Twilio integration"""
import os
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

_UNICODE_REPLACEMENTS = str.maketrans({
    "\u2026": "...", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-",   "\u2014": "--", "\u2022": "*", "\u00a0": " ",
    "\u2122": "(TM)", "\u00ae": "(R)", "\u00a9": "(C)",
})

def _sanitize_body(text: str) -> str:
    if not text:
        return text
    text = text.translate(_UNICODE_REPLACEMENTS)
    return text.replace("\x00", "")

try:
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False
    logger.warning("Twilio package not installed. SMS sending disabled.")


class SMSService:
    """Service for SMS campaign management with Twilio sending"""

    ACTIVE_STATUSES = {'queued', 'sending', 'processing'}
    BLOCKED_SEND_STATUSES = {'scheduled', 'sent', 'completed', 'failed', 'canceled', 'cancelled', 'archived'}
    ASYNC_RECIPIENT_THRESHOLD = int(os.environ.get('SMS_CAMPAIGN_ASYNC_THRESHOLD', '50'))
    _send_locks = {}
    _send_locks_guard = threading.Lock()

    @classmethod
    def _campaign_send_lock(cls, campaign_id):
        with cls._send_locks_guard:
            return cls._send_locks.setdefault(campaign_id, threading.Lock())

    @staticmethod
    def _audit_campaign_event(company_id, action, campaign_id, status=None):
        try:
            from extensions import db
            from models import IntegrationAuditLog
            db.session.add(IntegrationAuditLog(
                company_id=company_id,
                service_slug='sms_campaign',
                action=action,
                changes={'campaign_id': campaign_id, 'status': status},
            ))
        except Exception:
            logger.exception('Failed to write SMS campaign audit log')

    @classmethod
    def begin_send(cls, campaign_id, queued=False):
        from extensions import db
        from models import SMSCampaign
        from services.license_service import PHONE_PWA_FEATURE, has_feature

        # The per-process lock prevents double-click/rapid-repeat requests handled by this app worker.
        # The DB status transition below remains the cross-worker safety net; PostgreSQL also honors
        # the SELECT ... FOR UPDATE attempt while SQLite simply ignores it in tests.
        lock = cls._campaign_send_lock(campaign_id)
        with lock:
            query = SMSCampaign.query.filter_by(id=campaign_id)
            try:
                query = query.with_for_update(nowait=False)
            except Exception:
                pass
            campaign = query.first()
            if not campaign:
                return None, {'success': False, 'error': 'Campaign not found', 'status_code': 404}
            if campaign.company_id and not has_feature(campaign.company_id, PHONE_PWA_FEATURE):
                cls._audit_campaign_event(campaign.company_id, 'send_rejected_license_inactive', campaign.id, campaign.status)
                db.session.commit()
                return campaign, {
                    'success': False,
                    'error': 'Phone/PWA Communications license is not active.',
                    'status_code': 402,
                    'license_blocked': True,
                }
            if campaign.status in cls.ACTIVE_STATUSES or campaign.status in cls.BLOCKED_SEND_STATUSES:
                cls._audit_campaign_event(campaign.company_id, 'duplicate_send_rejected', campaign.id, campaign.status)
                db.session.commit()
                return campaign, {
                    'success': False,
                    'error': f'Campaign cannot be sent while status is {campaign.status}',
                    'duplicate': True,
                    'status_code': 409,
                }
            campaign.status = 'queued' if queued else 'sending'
            cls._audit_campaign_event(campaign.company_id, 'send_queued' if queued else 'send_started', campaign.id, campaign.status)
            db.session.commit()
            return campaign, {'success': True}

    @classmethod
    def queue_campaign_send(cls, campaign_id, app=None):
        campaign, result = cls.begin_send(campaign_id, queued=True)
        if not result.get('success'):
            return result

        def _runner():
            try:
                if app is not None:
                    with app.app_context():
                        cls.send_campaign(campaign_id, transition=False)
                else:
                    cls.send_campaign(campaign_id, transition=False)
            except Exception:
                logger.exception('Background SMS campaign send failed for campaign_id=%s', campaign_id)

        thread = threading.Thread(target=_runner, name=f'sms-campaign-{campaign_id}', daemon=True)
        thread.start()
        return {'success': True, 'queued': True, 'campaign_id': campaign_id}

    _twilio_client = None
    _twilio_phone = None
    _twilio_enabled = False
    
    @classmethod
    def _init_twilio(cls):
        """Initialize Twilio client if not already done"""
        if cls._twilio_client is not None:
            return cls._twilio_enabled
            
        if not TWILIO_AVAILABLE:
            cls._twilio_enabled = False
            return False
            
        try:
            from services.provider_config import get_provider_config
            account_sid   = get_provider_config('twilio', 'platform', 'account_sid')
            auth_token    = get_provider_config('twilio', 'platform', 'auth_token')
            # Phone routing inside the same try — DB-first, env bootstrap via resolver
            cls._twilio_phone = get_provider_config('twilio', 'platform', 'phone_number')
        except Exception:
            account_sid   = os.environ.get('TWILIO_ACCOUNT_SID')
            auth_token    = os.environ.get('TWILIO_AUTH_TOKEN')
            cls._twilio_phone = os.environ.get('TWILIO_PHONE_NUMBER')
        
        if account_sid and auth_token and cls._twilio_phone:
            try:
                cls._twilio_client = Client(account_sid, auth_token)
                cls._twilio_enabled = True
                logger.info("Twilio SMS client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Twilio: {e}")
                cls._twilio_enabled = False
        else:
            logger.warning("Twilio credentials not configured. SMS sending disabled.")
            cls._twilio_enabled = False
            
        return cls._twilio_enabled
    

    @staticmethod
    def _tenant_twilio_config(company_id):
        """Return tenant Twilio client/routing config when configured."""
        if not company_id or not TWILIO_AVAILABLE:
            return None
        try:
            from models import TwilioAccount
            ta = TwilioAccount.query.filter_by(company_id=company_id, is_active=True).first()
            if not ta or not ta.is_configured:
                return None
            account_sid = ta.get_account_sid()
            auth_token = ta.get_auth_token()
            if not account_sid or not auth_token:
                return None
            return {
                'client': Client(account_sid, auth_token),
                'messaging_service_sid': ta.messaging_service_sid,
                'from_phone': ta.from_phone,
            }
        except Exception as exc:
            logger.error("Failed to load tenant Twilio config for company_id=%s: %s", company_id, exc)
            return None

    @staticmethod
    def create_campaign(name, message, scheduled_at=None, company_id=None):
        """Create a persistent SMS marketing campaign."""
        from extensions import db
        from models import SMSCampaign

        message = SMSService.ensure_opt_out_language(message or "")
        status = 'scheduled' if scheduled_at else 'draft'
        campaign = SMSCampaign(
            company_id=company_id,
            name=name,
            message=message,
            status=status,
            scheduled_at=scheduled_at,
            created_at=datetime.utcnow()
        )
        db.session.add(campaign)
        db.session.commit()
        return campaign
    
    @staticmethod
    def add_recipients(campaign_id, contact_ids):
        """Add recipients to a campaign"""
        from extensions import db
        from models import SMSCampaign, SMSRecipient, Contact

        campaign = db.session.get(SMSCampaign, campaign_id)
        if not campaign:
            return

        for contact_id in contact_ids:
            contact = db.session.get(Contact, contact_id)
            if campaign.company_id and getattr(contact, 'company_id', None) != campaign.company_id:
                continue
            if not SMSService.contact_can_receive_marketing(contact):
                continue
            recipient = SMSRecipient(
                company_id=campaign.company_id,
                campaign_id=campaign_id,
                contact_id=contact_id,
                phone_number=contact.phone,
                status='pending'
            )
            db.session.add(recipient)
        db.session.commit()
    
    @staticmethod
    def ensure_opt_out_language(message):
        if not message:
            return message
        lower = message.lower()
        if "stop" in lower or "opt out" in lower or "unsubscribe" in lower:
            return message
        return f"{message.rstrip()} Reply STOP to opt out."

    @staticmethod
    def contact_can_receive_marketing(contact):
        if not contact or not getattr(contact, "phone", None):
            return False
        if getattr(contact, "do_not_market", False) or getattr(contact, "do_not_sms", False):
            return False
        if getattr(contact, "sms_opt_out_at", None) or getattr(contact, "sms_opted_out", False):
            return False
        return bool(
            getattr(contact, "sms_marketing_opt_in", False)
            and getattr(contact, "sms_consent_status", "unknown") in ("opted_in", "subscribed")
        )

    @staticmethod
    def create_template(name, message, category='promotional', tone='professional'):
        """Create a reusable SMS template"""
        from extensions import db
        from models import SMSTemplate
        
        template = SMSTemplate(
            name=name,
            message=message,
            category=category,
            tone=tone,
            created_at=datetime.utcnow()
        )
        db.session.add(template)
        db.session.commit()
        return template
    
    @classmethod
    def send_sms(cls, to_number, message, company_id=None, from_phone=None, media_urls=None):
        selected_from_phone = from_phone
        """Send an SMS message via tenant Twilio config, falling back to platform config."""
        if company_id:
            from services.license_service import PHONE_PWA_FEATURE, has_feature
            if not has_feature(company_id, PHONE_PWA_FEATURE):
                return {
                    'success': False,
                    'error': 'Phone/PWA Communications license is not active.',
                    'status_code': 402,
                    'license_blocked': True,
                }
        tenant_config = cls._tenant_twilio_config(company_id)
        if tenant_config:
            client = tenant_config['client']
            messaging_service_sid = tenant_config.get('messaging_service_sid')
            from_phone = selected_from_phone or tenant_config.get('from_phone')
        else:
            if not cls._init_twilio():
                return {
                    'success': False,
                    'error': 'Twilio not configured. Please configure tenant Twilio settings or set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_PHONE_NUMBER.'
                }
            client = cls._twilio_client
            messaging_service_sid = None
            from_phone = selected_from_phone or cls._twilio_phone
        
        try:
            clean_number = to_number.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
            if not clean_number.startswith('1') and len(clean_number) == 10:
                clean_number = '1' + clean_number
            formatted_number = '+' + clean_number
            
            message = _sanitize_body(cls.ensure_opt_out_language(message))
            send_kwargs = {'body': message, 'to': formatted_number}
            if media_urls:
                send_kwargs['media_url'] = media_urls
            if messaging_service_sid and not selected_from_phone:
                send_kwargs['messaging_service_sid'] = messaging_service_sid
            else:
                send_kwargs['from_'] = from_phone
            message_obj = client.messages.create(**send_kwargs)
            
            logger.info(f"SMS sent successfully. SID: {message_obj.sid}")
            return {
                'success': True,
                'message_sid': message_obj.sid,
                'status': message_obj.status
            }
            
        except Exception as e:
            logger.error(f"Error sending SMS to {to_number}: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    @classmethod
    def execute_scheduled_campaign(cls, campaign_id):
        """Worker entry point; all tenant context comes from the campaign row."""
        from extensions import db
        from models import SMSCampaign
        from services.contact_audience import resolve_sms_campaign_recipients
        from services.phone_line_service import PhoneLineService

        campaign = db.session.get(SMSCampaign, campaign_id)
        if not campaign or campaign.status != "scheduled":
            return {"success": False, "error": "Scheduled campaign not found or not scheduled"}
        sender = PhoneLineService.resolve_campaign_sender(
            campaign.company_id, campaign.from_phone_number_id, user=None
        )
        if not sender.get("success"):
            campaign.status = "failed"
            db.session.commit()
            return sender
        result = resolve_sms_campaign_recipients(campaign, materialize=True)
        counts = result["counts"]
        campaign.recipient_resolution = counts
        campaign.execution_recipient_count = counts["eligible_recipients"]
        campaign.execution_count_delta = counts["eligible_recipients"] - (campaign.scheduled_eligible_recipient_count or 0)
        if not counts["eligible_recipients"]:
            campaign.status = "failed"
            db.session.commit()
            return {"success": False, "error": counts["explanation"], "counts": counts, "sent": 0}
        campaign.status = "draft"  # allow the guarded canonical sender to transition it
        db.session.commit()
        return cls.send_campaign(campaign.id)

    @classmethod
    def send_campaign(cls, campaign_id, transition=True):
        """Send SMS campaign to all pending recipients."""
        from extensions import db
        from models import SMSCampaign, SMSRecipient

        if transition:
            campaign, begin_result = cls.begin_send(campaign_id, queued=False)
            if not begin_result.get('success'):
                return begin_result
        else:
            campaign = db.session.get(SMSCampaign, campaign_id)
            if not campaign:
                return {'success': False, 'error': 'Campaign not found'}
            campaign.status = 'sending'
            db.session.commit()
        
        recipients = (
            SMSRecipient.query
            .filter_by(campaign_id=campaign_id, status='pending')
            .order_by(SMSRecipient.id.asc())
            .limit(max(1, int(getattr(campaign, 'batch_size', None) or 50)))
            .all()
        )
        
        sent = 0
        failed = 0
        
        for recipient in recipients:
            try:
                result = cls.send_sms(recipient.phone_number, campaign.message, company_id=campaign.company_id, from_phone=getattr(campaign, 'from_phone_number', None), media_urls=getattr(campaign, 'media_urls', None))
            except TypeError:
                # Backward-compatible for tests/integrations monkeypatching send_sms with the legacy signature.
                result = cls.send_sms(recipient.phone_number, campaign.message, company_id=campaign.company_id)
            if result['success']:
                recipient.status = 'sent'
                recipient.sent_at = datetime.utcnow()
                recipient.message_sid = result.get('message_sid')
                recipient.provider_message_sid = result.get('message_sid')
                sent += 1
            else:
                recipient.status = 'failed'
                recipient.error_message = result.get('error', 'Unknown error')
                failed += 1
        
        remaining = SMSRecipient.query.filter_by(campaign_id=campaign_id, status='pending').count()
        if remaining:
            campaign.status = 'scheduled'
        elif failed > 0 and sent == 0:
            campaign.status = 'failed'
        else:
            campaign.status = 'completed'
            campaign.sent_at = datetime.utcnow()
        db.session.commit()
        
        return {
            'success': sent > 0,
            'sent': sent,
            'failed': failed,
            'total': len(recipients)
        }
    
    @staticmethod
    def ai_generate_sms(prompt, tone='professional', max_length=160):
        """Generate SMS content using AI"""
        try:
            from ai_agent import get_lux_agent
            lux_agent = get_lux_agent()
            
            full_prompt = f"""Create a short SMS marketing message (max {max_length} chars) with a {tone} tone.
            Topic: {prompt}
            
            Requirements:
            - Must be under {max_length} characters
            - Include a clear call-to-action
            - Be engaging and compelling
            - End with "Reply STOP to unsubscribe" if promotional
            
            Return ONLY the SMS message text, nothing else."""
            
            content = lux_agent.generate_email_content(full_prompt, "sms")
            
            if isinstance(content, dict):
                message = content.get('content', content.get('message', str(content)))
            else:
                message = str(content)
            
            if len(message) > max_length:
                message = message[:max_length-3] + '...'
            
            return message
            
        except Exception as e:
            logger.error(f"AI SMS generation error: {e}")
            return f"{prompt[:100]}... Reply STOP to opt out."
    
    @staticmethod
    def check_compliance(message):
        """Check if SMS message is compliant"""
        issues = []
        
        if len(message) > 160:
            issues.append('Message exceeds 160 characters')
        
        opt_out_keywords = ['stop', 'unsubscribe', 'opt out', 'optout']
        has_opt_out = any(kw in message.lower() for kw in opt_out_keywords)
        if not has_opt_out:
            issues.append('Missing opt-out instructions (e.g., "Reply STOP to unsubscribe")')
        
        return {
            'compliant': len(issues) == 0,
            'issues': issues,
            'length': len(message),
            'segments': (len(message) // 160) + 1
        }
    
    @staticmethod
    def calculate_analytics(campaign_id):
        """Calculate analytics for an SMS campaign"""
        from models import SMSCampaign, SMSRecipient
        
        campaign = db.session.get(SMSCampaign, campaign_id)
        if not campaign:
            return {}
        
        recipients = SMSRecipient.query.filter_by(campaign_id=campaign_id).all()
        
        total = len(recipients)
        sent = len([r for r in recipients if r.status == 'sent'])
        failed = len([r for r in recipients if r.status == 'failed'])
        pending = len([r for r in recipients if r.status == 'pending'])
        
        return {
            'total_recipients': total,
            'sent': sent,
            'failed': failed,
            'pending': pending,
            'delivery_rate': (sent / total * 100) if total > 0 else 0,
            'campaign': campaign
        }
