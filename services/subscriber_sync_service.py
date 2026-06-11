"""
Subscriber Sync Service
Handles synchronization between contacts and newsletter subscribers
"""
import logging
from datetime import datetime
from models import db, Contact

logger = logging.getLogger(__name__)


class SubscriberSyncService:
    """Service to sync contacts with newsletter subscribers"""
    
    @staticmethod
    def sync_contacts_to_subscribers():
        """
        Sync all contacts with segment='newsletter' to be subscribers.
        Also ensures is_subscribed field is consistent with segment.
        """
        try:
            synced_count = 0
            
            newsletter_contacts = Contact.query.filter(
                Contact.segment == 'newsletter',
                Contact.is_subscribed == False,
                Contact.is_active == True
            ).all()
            
            for contact in newsletter_contacts:
                contact.is_subscribed = True
                contact.subscribed_at = contact.subscribed_at or datetime.utcnow()
                contact.subscription_source = contact.subscription_source or 'sync'
                synced_count += 1
            
            if synced_count > 0:
                db.session.commit()
                logger.info(f"Synced {synced_count} newsletter contacts to subscribers")
            
            return {
                'success': True,
                'synced_count': synced_count,
                'message': f'Synced {synced_count} contacts to subscribers'
            }
        except Exception as e:
            logger.error(f"Error syncing contacts to subscribers: {e}")
            db.session.rollback()
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def sync_subscribers_to_contacts():
        """
        Sync all subscribed contacts to have newsletter segment if not already set.
        """
        try:
            synced_count = 0
            
            subscribed_not_newsletter = Contact.query.filter(
                Contact.is_subscribed == True,
                Contact.segment != 'newsletter',
                Contact.is_active == True
            ).all()
            
            for contact in subscribed_not_newsletter:
                existing_tags = contact.tags or ''
                if 'newsletter' not in existing_tags.lower():
                    if existing_tags:
                        contact.tags = f"{existing_tags},newsletter"
                    else:
                        contact.tags = 'newsletter'
                synced_count += 1
            
            if synced_count > 0:
                db.session.commit()
                logger.info(f"Added newsletter tag to {synced_count} subscribed contacts")
            
            return {
                'success': True,
                'synced_count': synced_count,
                'message': f'Added newsletter tag to {synced_count} contacts'
            }
        except Exception as e:
            logger.error(f"Error syncing subscribers to contacts: {e}")
            db.session.rollback()
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def subscribe_contact(contact_id, source='manual'):
        """Subscribe a contact to the newsletter"""
        try:
            contact = db.session.get(Contact, contact_id)
            if not contact:
                return {'success': False, 'error': 'Contact not found'}
            
            contact.is_subscribed = True
            contact.subscribed_at = datetime.utcnow()
            contact.unsubscribed_at = None
            contact.subscription_source = source
            
            if contact.segment == 'lead':
                contact.segment = 'newsletter'
            
            existing_tags = contact.tags or ''
            if 'newsletter' not in existing_tags.lower():
                if existing_tags:
                    contact.tags = f"{existing_tags},newsletter"
                else:
                    contact.tags = 'newsletter'
            
            db.session.commit()
            logger.info(f"Contact {contact.email} subscribed to newsletter")
            
            return {
                'success': True,
                'contact_id': contact.id,
                'email': contact.email,
                'message': f'{contact.email} subscribed successfully'
            }
        except Exception as e:
            logger.error(f"Error subscribing contact: {e}")
            db.session.rollback()
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def unsubscribe_contact(contact_id):
        """Unsubscribe a contact from the newsletter"""
        try:
            contact = db.session.get(Contact, contact_id)
            if not contact:
                return {'success': False, 'error': 'Contact not found'}
            
            contact.is_subscribed = False
            contact.unsubscribed_at = datetime.utcnow()
            
            db.session.commit()
            logger.info(f"Contact {contact.email} unsubscribed from newsletter")
            
            return {
                'success': True,
                'contact_id': contact.id,
                'email': contact.email,
                'message': f'{contact.email} unsubscribed successfully'
            }
        except Exception as e:
            logger.error(f"Error unsubscribing contact: {e}")
            db.session.rollback()
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def bulk_subscribe(contact_ids, source='bulk'):
        """Subscribe multiple contacts at once"""
        try:
            subscribed_count = 0
            
            for contact_id in contact_ids:
                result = SubscriberSyncService.subscribe_contact(contact_id, source)
                if result.get('success'):
                    subscribed_count += 1
            
            return {
                'success': True,
                'subscribed_count': subscribed_count,
                'total': len(contact_ids),
                'message': f'Subscribed {subscribed_count} of {len(contact_ids)} contacts'
            }
        except Exception as e:
            logger.error(f"Error bulk subscribing: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def bulk_unsubscribe(contact_ids):
        """Unsubscribe multiple contacts at once"""
        try:
            unsubscribed_count = 0
            
            for contact_id in contact_ids:
                result = SubscriberSyncService.unsubscribe_contact(contact_id)
                if result.get('success'):
                    unsubscribed_count += 1
            
            return {
                'success': True,
                'unsubscribed_count': unsubscribed_count,
                'total': len(contact_ids),
                'message': f'Unsubscribed {unsubscribed_count} of {len(contact_ids)} contacts'
            }
        except Exception as e:
            logger.error(f"Error bulk unsubscribing: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def get_subscriber_stats():
        """Get subscriber statistics"""
        try:
            total_contacts = Contact.query.filter_by(is_active=True).count()
            total_subscribers = Contact.query.filter_by(is_subscribed=True, is_active=True).count()
            newsletter_segment = Contact.query.filter_by(segment='newsletter', is_active=True).count()
            unsubscribed = Contact.query.filter(
                Contact.unsubscribed_at.isnot(None),
                Contact.is_active == True
            ).count()
            
            recent_subscribers = Contact.query.filter(
                Contact.is_subscribed == True,
                Contact.subscribed_at.isnot(None)
            ).order_by(Contact.subscribed_at.desc()).limit(10).all()
            
            return {
                'success': True,
                'stats': {
                    'total_contacts': total_contacts,
                    'total_subscribers': total_subscribers,
                    'newsletter_segment': newsletter_segment,
                    'unsubscribed_count': unsubscribed,
                    'subscription_rate': round((total_subscribers / total_contacts * 100) if total_contacts > 0 else 0, 2)
                },
                'recent_subscribers': [
                    {
                        'id': c.id,
                        'email': c.email,
                        'name': c.full_name,
                        'subscribed_at': c.subscribed_at.isoformat() if c.subscribed_at else None,
                        'source': c.subscription_source
                    }
                    for c in recent_subscribers
                ]
            }
        except Exception as e:
            logger.error(f"Error getting subscriber stats: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def get_all_subscribers(page=1, per_page=50):
        """Get all subscribers with pagination"""
        try:
            subscribers = Contact.query.filter_by(
                is_subscribed=True,
                is_active=True
            ).order_by(Contact.subscribed_at.desc()).paginate(
                page=page, per_page=per_page, error_out=False
            )
            
            return {
                'success': True,
                'subscribers': [
                    {
                        'id': c.id,
                        'email': c.email,
                        'first_name': c.first_name,
                        'last_name': c.last_name,
                        'full_name': c.full_name,
                        'segment': c.segment,
                        'tags': c.tags,
                        'subscribed_at': c.subscribed_at.isoformat() if c.subscribed_at else None,
                        'source': c.subscription_source,
                        'engagement_score': c.engagement_score
                    }
                    for c in subscribers.items
                ],
                'pagination': {
                    'page': subscribers.page,
                    'per_page': subscribers.per_page,
                    'total': subscribers.total,
                    'pages': subscribers.pages,
                    'has_next': subscribers.has_next,
                    'has_prev': subscribers.has_prev
                }
            }
        except Exception as e:
            logger.error(f"Error getting subscribers: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def full_sync():
        """Run full bidirectional sync between contacts and subscribers"""
        try:
            result1 = SubscriberSyncService.sync_contacts_to_subscribers()
            result2 = SubscriberSyncService.sync_subscribers_to_contacts()
            
            return {
                'success': True,
                'contacts_to_subscribers': result1.get('synced_count', 0),
                'subscribers_to_contacts': result2.get('synced_count', 0),
                'message': 'Full sync completed successfully'
            }
        except Exception as e:
            logger.error(f"Error in full sync: {e}")
            return {'success': False, 'error': str(e)}
