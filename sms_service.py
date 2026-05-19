"""SMS Service for Twilio integration."""
import os
import logging
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

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
    return text.encode("latin-1", errors="replace").decode("latin-1")


class SMSService:
    """Service to handle SMS operations via Twilio."""
    
    def __init__(self):
        self.account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
        self.auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
        self.phone_number = os.environ.get('TWILIO_PHONE_NUMBER')
        
        if self.account_sid and self.auth_token and self.phone_number:
            self.client = Client(self.account_sid, self.auth_token)
            self.enabled = True
        else:
            self.client = None
            self.enabled = False
            logger.warning("Twilio credentials not fully configured. SMS features disabled. Need: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER")
    
    def send_sms(self, to_number, message):
        """
        Send an SMS message to a single recipient.
        
        Args:
            to_number (str): Recipient phone number in E.164 format (+1234567890)
            message (str): Message content (max 160 characters for single SMS)
        
        Returns:
            dict: Result with 'success', 'message_sid', and 'error' keys
        """
        if not self.enabled:
            return {
                'success': False,
                'error': 'Twilio not configured. Please set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_PHONE_NUMBER.'
            }
        
        try:
            # Normalize and validate phone number
            if not self.validate_phone_number(to_number):
                return {
                    'success': False,
                    'error': f'Invalid phone number format: {to_number}'
                }
            
            # Clean and format to E.164
            clean_number = to_number.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
            if not clean_number.startswith('1') and len(clean_number) == 10:
                # Add US country code if missing
                clean_number = '1' + clean_number
            formatted_number = '+' + clean_number
            
            message = _sanitize_body(message)
            message_obj = self.client.messages.create(
                body=message,
                from_=self.phone_number,
                to=formatted_number
            )
            
            logger.info(f"SMS sent successfully. SID: {message_obj.sid}")
            return {
                'success': True,
                'message_sid': message_obj.sid,
                'status': message_obj.status
            }
            
        except TwilioRestException as e:
            logger.error(f"Twilio error sending SMS to {to_number}: {e}")
            return {
                'success': False,
                'error': str(e)
            }
        except Exception as e:
            logger.error(f"Error sending SMS to {to_number}: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def send_bulk_sms(self, recipients, message):
        """
        Send SMS to multiple recipients.
        
        Args:
            recipients (list): List of phone numbers in E.164 format
            message (str): Message content
        
        Returns:
            dict: Results with 'sent', 'failed', and 'results' keys
        """
        if not self.enabled:
            return {
                'sent': 0,
                'failed': len(recipients),
                'error': 'Twilio not configured'
            }
        
        results = {
            'sent': 0,
            'failed': 0,
            'results': []
        }
        
        for phone in recipients:
            result = self.send_sms(phone, message)
            if result['success']:
                results['sent'] += 1
            else:
                results['failed'] += 1
            results['results'].append({
                'phone': phone,
                'result': result
            })
        
        return results
    
    def validate_phone_number(self, phone_number):
        """
        Validate phone number format.
        
        Args:
            phone_number (str): Phone number to validate
        
        Returns:
            bool: True if valid, False otherwise
        """
        if not phone_number:
            return False
        
        # Basic validation: should be 10-15 digits with optional + prefix
        clean = phone_number.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
        return clean.isdigit() and 10 <= len(clean) <= 15
