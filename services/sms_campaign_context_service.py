"""SMS campaign context for analytics, reports, calendar, and AI assistants."""
from __future__ import annotations

from datetime import datetime, timedelta


class SMSCampaignContextService:
    @staticmethod
    def ai_context(company_id: int, limit: int = 10):
        from models import SMSCampaign, SMSRecipient
        if not company_id:
            return {'scheduled': [], 'recent': [], 'suggestions': []}
        campaigns = (SMSCampaign.query
                     .filter_by(company_id=company_id)
                     .order_by(SMSCampaign.updated_at.desc())
                     .limit(limit)
                     .all())
        scheduled = []
        recent = []
        suggestions = []
        for campaign in campaigns:
            metrics = SMSCampaignContextService.metrics(campaign.id, company_id)
            payload = {
                'id': campaign.id,
                'name': campaign.name,
                'status': campaign.status,
                'scheduled_at': campaign.scheduled_at.isoformat() if campaign.scheduled_at else None,
                'from_phone_number': campaign.from_phone_number,
                'metrics': metrics,
            }
            if campaign.status == 'scheduled':
                scheduled.append(payload)
            else:
                recent.append(payload)
            if metrics['failed'] > metrics['sent']:
                suggestions.append(f"Review sender reputation and content for '{campaign.name}' because failures exceed sends.")
            if campaign.status == 'draft' and not campaign.from_phone_number:
                suggestions.append(f"Choose a compliant sender phone line before scheduling '{campaign.name}'.")
        return {'scheduled': scheduled, 'recent': recent, 'suggestions': suggestions}

    @staticmethod
    def metrics(campaign_id: int, company_id: int):
        from models import SMSRecipient
        rows = SMSRecipient.query.filter_by(campaign_id=campaign_id, company_id=company_id).all()
        return {
            'recipients': len(rows),
            'pending': sum(1 for r in rows if r.status == 'pending'),
            'sent': sum(1 for r in rows if r.status in ('sent', 'delivered')),
            'failed': sum(1 for r in rows if r.status == 'failed'),
            'opted_out': sum(1 for r in rows if r.opted_out_at is not None),
        }
