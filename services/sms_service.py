"""SMS Service for SMS campaign management with Twilio integration"""
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False
    logger.warning("Twilio package not installed. SMS sending disabled.")


class SMSService:
    """Service for SMS campaign management with Twilio sending"""
    
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
            
        account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
        auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
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
    def create_campaign(name, message, scheduled_at=None):
        """Create a new SMS campaign"""
        from extensions import db
        from models import SMSCampaign
        
        status = 'scheduled' if scheduled_at else 'draft'
        campaign = SMSCampaign(
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
        from models import SMSRecipient, Contact
        
        for contact_id in contact_ids:
            contact = Contact.query.get(contact_id)
            if contact and contact.phone:
                recipient = SMSRecipient(
                    campaign_id=campaign_id,
                    contact_id=contact_id,
                    phone_number=contact.phone,
                    status='pending'
                )
                db.session.add(recipient)
        db.session.commit()
    
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
    def send_sms(cls, to_number, message):
        """Send an SMS message via Twilio"""
        if not cls._init_twilio():
            return {
                'success': False,
                'error': 'Twilio not configured. Please set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_PHONE_NUMBER.'
            }
        
        try:
            clean_number = to_number.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
            if not clean_number.startswith('1') and len(clean_number) == 10:
                clean_number = '1' + clean_number
            formatted_number = '+' + clean_number
            
            message_obj = cls._twilio_client.messages.create(
                body=message,
                from_=cls._twilio_phone,
                to=formatted_number
            )
            
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
    def send_campaign(cls, campaign_id):
        """Send SMS campaign to all recipients"""
        from extensions import db
        from models import SMSCampaign, SMSRecipient
        
        campaign = SMSCampaign.query.get(campaign_id)
        if not campaign:
            return {'success': False, 'error': 'Campaign not found'}
        
        recipients = SMSRecipient.query.filter_by(campaign_id=campaign_id, status='pending').all()
        
        sent = 0
        failed = 0
        
        for recipient in recipients:
            result = cls.send_sms(recipient.phone_number, campaign.message)
            if result['success']:
                recipient.status = 'sent'
                recipient.sent_at = datetime.utcnow()
                recipient.message_sid = result.get('message_sid')
                sent += 1
            else:
                recipient.status = 'failed'
                recipient.error_message = result.get('error', 'Unknown error')
                failed += 1
        
        if failed > 0 and sent == 0:
            campaign.status = 'failed'
        elif failed > 0:
            campaign.status = 'partial'
            campaign.sent_at = datetime.utcnow()
        else:
            campaign.status = 'sent'
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
        
        campaign = SMSCampaign.query.get(campaign_id)
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
