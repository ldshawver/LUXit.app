import re
import logging
from datetime import datetime

from extensions import db

def validate_email(email):
    """Validate email address format"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def format_datetime(dt):
    """Format datetime for display"""
    if not dt:
        return 'N/A'
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def format_date(dt):
    """Format date for display"""
    if not dt:
        return 'N/A'
    return dt.strftime('%Y-%m-%d')

def calculate_open_rate(sent_count, opened_count):
    """Calculate email open rate percentage"""
    if sent_count == 0:
        return 0
    return round((opened_count / sent_count) * 100, 2)

def calculate_click_rate(sent_count, clicked_count):
    """Calculate email click rate percentage"""
    if sent_count == 0:
        return 0
    return round((clicked_count / sent_count) * 100, 2)

def sanitize_filename(filename):
    """Sanitize filename for safe storage"""
    # Remove unsafe characters
    filename = re.sub(r'[^\w\s-]', '', filename)
    # Replace spaces with underscores
    filename = re.sub(r'[-\s]+', '_', filename)
    return filename.strip('_')

def log_activity(user_id, action, details=None):
    """Log user activity"""
    logging.info(f"User {user_id}: {action} - {details or ''}")

def parse_tags(tags_string):
    """Parse comma-separated tags string"""
    if not tags_string:
        return []
    return [tag.strip() for tag in tags_string.split(',') if tag.strip()]

def tags_to_string(tags_list):
    """Convert tags list to comma-separated string"""
    if not tags_list:
        return ''
    return ', '.join(tags_list)

def get_campaign_status_color(status):
    """Get Bootstrap color class for campaign status"""
    status_colors = {
        'draft': 'secondary',
        'scheduled': 'warning',
        'sending': 'info',
        'sent': 'success',
        'failed': 'danger',
        'paused': 'dark',
        'partial': 'warning'
    }
    return status_colors.get(status, 'secondary')

def truncate_text(text, max_length=50):
    """Truncate text to specified length"""
    if not text:
        return ''
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + '...'


def safe_count(query, fallback=0, context=""):
    """Safely count query results without failing the caller."""
    logger = logging.getLogger(__name__)
    try:
        return query.count()
    except Exception as exc:
        logger.warning("Dashboard metric query failed%s: %s", f" ({context})" if context else "", exc)
        try:
            db.session.rollback()
        except Exception:
            pass
        return fallback
