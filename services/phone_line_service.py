"""Per-number phone-line routing and campaign sender safety helpers."""
from __future__ import annotations

from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def normalize_phone(number: str | None) -> str | None:
    if not number:
        return None
    digits = ''.join(ch for ch in str(number) if ch.isdigit())
    if len(digits) == 10:
        digits = '1' + digits
    return f'+{digits}' if digits else None


class PhoneLineService:
    """Resolve inbound and outbound communications by exact tenant phone line."""

    @staticmethod
    def resolve_by_to_number(to_number: str, purpose: str = 'sms'):
        from extensions import db
        from models import TwilioPhoneNumber, TwilioAccount, IntegrationAuditLog

        normalized = normalize_phone(to_number)
        line = TwilioPhoneNumber.query.filter_by(phone_number=normalized, is_active=True).first()
        if line:
            source = 'phone_number_settings'
            company_id = line.company_id
            settings = line
        else:
            source = 'unresolved'
            company_id = None
            settings = None

        try:
            db.session.add(IntegrationAuditLog(
                company_id=company_id,
                service_slug='phone_line_resolution',
                action=f'resolve_inbound_{purpose}',
                changes={'to_number': normalized, 'source': source, 'phone_number_id': getattr(line, 'id', None)},
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception('Unable to audit phone-line resolution')
        return {'company_id': company_id, 'phone_number': line, 'settings': settings, 'source': source}

    @staticmethod
    def campaign_sender_options(company_id: int):
        from models import TwilioPhoneNumber
        if not company_id:
            return []
        return (TwilioPhoneNumber.query
                .filter_by(company_id=company_id, is_active=True, sms_enabled=True, campaign_sender_enabled=True)
                .order_by(TwilioPhoneNumber.is_primary.desc(), TwilioPhoneNumber.phone_number.asc())
                .all())

    @staticmethod
    def resolve_campaign_sender(company_id: int, sender_number_id: int | None = None, allow_fallback: bool = False, user=None):
        from extensions import db
        from models import TwilioPhoneNumber, TwilioAccount, IntegrationAuditLog

        line = None
        source = 'none'
        warning = None
        if sender_number_id:
            line = TwilioPhoneNumber.query.filter_by(
                id=sender_number_id,
                company_id=company_id,
                is_active=True,
                sms_enabled=True,
                campaign_sender_enabled=True,
            ).first()
            if not line:
                return {'success': False, 'error': 'Selected sender number is not available for this business.'}
            if user is not None:
                from services.comms_permissions import accessible_phone_numbers
                allowed_numbers = accessible_phone_numbers(user, company_id)
                if line.phone_number not in allowed_numbers:
                    return {'success': False, 'error': 'You do not have permission to use that sender number.'}
            source = 'phone_number_settings'
        else:
            line = (TwilioPhoneNumber.query
                    .filter_by(company_id=company_id, is_active=True, sms_enabled=True, campaign_sender_enabled=True, is_primary=True)
                    .first())
            if line:
                if user is not None:
                    from services.comms_permissions import accessible_phone_numbers
                    allowed_numbers = accessible_phone_numbers(user, company_id)
                    if line.phone_number not in allowed_numbers:
                        return {'success': False, 'error': 'You do not have permission to use the primary sender number.'}
                source = 'primary_phone_number'
            elif allow_fallback:
                acct = TwilioAccount.query.filter_by(company_id=company_id, is_active=True).first()
                if acct and acct.from_phone:
                    source = 'explicit_global_fallback'
                    warning = 'Using explicitly allowed global/API Twilio fallback sender.'
                    return {'success': True, 'phone_number': None, 'from_phone': acct.from_phone, 'source': source, 'warning': warning}
            else:
                return {'success': False, 'error': 'Choose a permitted SMS sender number before scheduling or sending this campaign.'}

        try:
            db.session.add(IntegrationAuditLog(
                company_id=company_id,
                service_slug='phone_line_resolution',
                action='resolve_campaign_sender',
                changes={'phone_number_id': getattr(line, 'id', None), 'source': source, 'warning': warning},
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception('Unable to audit campaign sender resolution')
        return {'success': True, 'phone_number': line, 'from_phone': getattr(line, 'phone_number', None), 'source': source, 'warning': warning}
