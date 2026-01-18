"""
Email tracking utilities for opens, clicks, and engagement metrics
"""
import base64
from datetime import datetime
from urllib.parse import urlencode
from flask import request, url_for
from models import EmailTracking, CampaignRecipient, db
import logging

logger = logging.getLogger(__name__)

def generate_tracking_pixel(campaign_id, contact_id):
    """Generate a tracking pixel URL for email opens"""
    # Encode campaign and contact IDs
    tracking_data = f"{campaign_id}:{contact_id}"
    encoded_data = base64.urlsafe_b64encode(tracking_data.encode()).decode()
    
    return url_for('main.track_open', tracking_id=encoded_data)

def generate_tracking_link(original_url, campaign_id, contact_id):
    """Generate a tracking link for email clicks"""
    # Encode campaign and contact IDs
    tracking_data = f"{campaign_id}:{contact_id}"
    encoded_data = base64.urlsafe_b64encode(tracking_data.encode()).decode()
    
    params = {
        'tracking_id': encoded_data,
        'url': original_url
    }
    
    return url_for('main.track_click', **params)

def decode_tracking_data(tracking_id):
    """Decode tracking data from tracking ID"""
    try:
        decoded = base64.urlsafe_b64decode(tracking_id.encode()).decode()
        campaign_id, contact_id = decoded.split(':')
        return int(campaign_id), int(contact_id)
    except Exception as e:
        logger.error(f"Error decoding tracking data: {e}")
        return None, None

def record_email_event(campaign_id, contact_id, event_type, event_data=None):
    """Record an email tracking event"""
    try:
        # Create tracking record
        tracking = EmailTracking(
            campaign_id=campaign_id,
            contact_id=contact_id,
            event_type=event_type,
            event_data=event_data or {}
        )
        db.session.add(tracking)
        
        # Update campaign recipient record
        recipient = CampaignRecipient.query.filter_by(
            campaign_id=campaign_id,
            contact_id=contact_id
        ).first()
        
        if recipient:
            now = datetime.utcnow()
            if event_type == 'opened' and not recipient.opened_at:
                recipient.opened_at = now
            elif event_type == 'clicked' and not recipient.clicked_at:
                recipient.clicked_at = now
        
        db.session.commit()
        logger.info(f"Recorded {event_type} event for campaign {campaign_id}, contact {contact_id}")
        
    except Exception as e:
        logger.error(f"Error recording email event: {e}")
        db.session.rollback()

def process_email_content(html_content, campaign_id, contact_id):
    """Process email HTML content to add tracking pixels and links"""
    try:
        # Add tracking pixel (invisible 1x1 image)
        tracking_pixel_url = generate_tracking_pixel(campaign_id, contact_id)
        tracking_pixel = f'<img src="{tracking_pixel_url}" width="1" height="1" style="display:none;" alt="">'
        
        # Add tracking pixel before closing body tag
        if '</body>' in html_content:
            html_content = html_content.replace('</body>', f'{tracking_pixel}</body>')
        else:
            html_content += tracking_pixel
        
        # Process links for click tracking (basic implementation)
        # Note: For production, use a proper HTML parser like BeautifulSoup
        import re
        
        def replace_link(match):
            full_tag = match.group(0)
            href_match = re.search(r'href="([^"]+)"', full_tag)
            if href_match:
                original_url = href_match.group(1)
                # Skip mailto links and already tracked links
                if not original_url.startswith('mailto:') and 'track_click' not in original_url:
                    tracking_url = generate_tracking_link(original_url, campaign_id, contact_id)
                    return full_tag.replace(f'href="{original_url}"', f'href="{tracking_url}"')
            return full_tag
        
        # Replace all <a> tags with tracking links
        html_content = re.sub(r'<a[^>]*href="[^"]*"[^>]*>', replace_link, html_content)
        
        return html_content
        
    except Exception as e:
        logger.error(f"Error processing email content for tracking: {e}")
        return html_content

def get_campaign_analytics(campaign_id):
    """Get detailed analytics for a specific campaign"""
    try:
        from models import Campaign, CampaignRecipient
        
        campaign = Campaign.query.get(campaign_id)
        if not campaign:
            return None
        
        # Basic counts
        total_recipients = campaign.recipients.count()
        sent_count = campaign.recipients.filter_by(status='sent').count()
        failed_count = campaign.recipients.filter_by(status='failed').count()
        bounced_count = campaign.recipients.filter_by(status='bounced').count()
        
        # Engagement counts
        opened_count = campaign.recipients.filter(CampaignRecipient.opened_at.isnot(None)).count()
        clicked_count = campaign.recipients.filter(CampaignRecipient.clicked_at.isnot(None)).count()
        
        # Calculate rates
        delivery_rate = (sent_count / total_recipients * 100) if total_recipients > 0 else 0
        open_rate = (opened_count / sent_count * 100) if sent_count > 0 else 0
        click_rate = (clicked_count / sent_count * 100) if sent_count > 0 else 0
        bounce_rate = (bounced_count / (sent_count + bounced_count) * 100) if (sent_count + bounced_count) > 0 else 0
        
        # Event breakdown
        events = db.session.query(
            EmailTracking.event_type,
            db.func.count(EmailTracking.id).label('count')
        ).filter_by(campaign_id=campaign_id).group_by(EmailTracking.event_type).all()
        
        event_counts = {event.event_type: event.count for event in events}
        
        return {
            'campaign': campaign,
            'total_recipients': total_recipients,
            'sent_count': sent_count,
            'failed_count': failed_count,
            'bounced_count': bounced_count,
            'opened_count': opened_count,
            'clicked_count': clicked_count,
            'delivery_rate': delivery_rate,
            'open_rate': open_rate,
            'click_rate': click_rate,
            'bounce_rate': bounce_rate,
            'event_counts': event_counts
        }
        
    except Exception as e:
        logger.error(f"Error getting campaign analytics: {e}")
        return None
