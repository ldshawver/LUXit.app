"""
Automation Service - Phase 5
Handles automation testing, trigger library, and A/B testing
"""

from datetime import datetime
from extensions import db
from models import (Automation, AutomationTest, AutomationTriggerLibrary, 
                    AutomationABTest, Contact)
import logging

logger = logging.getLogger(__name__)

class AutomationService:
    @staticmethod
    def run_test(automation_id, test_contact_id=None, test_data=None):
        """Run automation in test mode"""
        try:
            automation = Automation.query.get(automation_id)
            if not automation:
                return None
            
            test = AutomationTest(
                automation_id=automation_id,
                test_contact_id=test_contact_id,
                test_data=test_data or {},
                status='running',
                started_at=datetime.utcnow()
            )
            db.session.add(test)
            db.session.commit()
            
            # Simulate test execution
            test_results = []
            for step in automation.steps:
                test_results.append({
                    'step_id': step.id,
                    'step_type': step.step_type,
                    'status': 'success',
                    'message': f'{step.step_type} would be executed'
                })
            
            test.test_results = test_results
            test.status = 'completed'
            test.completed_at = datetime.utcnow()
            db.session.commit()
            
            return test
        except Exception as e:
            logger.error(f"Error running automation test: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def create_trigger_template(name, trigger_type, description, category, trigger_config, steps_template):
        """Create pre-built trigger template"""
        try:
            template = AutomationTriggerLibrary(
                name=name,
                trigger_type=trigger_type,
                description=description,
                category=category,
                trigger_config=trigger_config,
                steps_template=steps_template
            )
            db.session.add(template)
            db.session.commit()
            return template
        except Exception as e:
            logger.error(f"Error creating trigger template: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def get_trigger_library(category=None):
        """Get available trigger templates"""
        try:
            query = AutomationTriggerLibrary.query
            if category:
                query = query.filter_by(category=category)
            return query.order_by(AutomationTriggerLibrary.usage_count.desc()).all()
        except Exception as e:
            logger.error(f"Error getting trigger library: {e}")
            return []
    
    @staticmethod
    def create_ab_test(automation_id, step_id, variant_a_id, variant_b_id, split=50):
        """Create A/B test for automation step"""
        try:
            ab_test = AutomationABTest(
                automation_id=automation_id,
                step_id=step_id,
                variant_a_template_id=variant_a_id,
                variant_b_template_id=variant_b_id,
                split_percentage=split
            )
            db.session.add(ab_test)
            db.session.commit()
            return ab_test
        except Exception as e:
            logger.error(f"Error creating A/B test: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def update_ab_test_results(test_id, variant, sent=0, opens=0, clicks=0):
        """Update A/B test results"""
        try:
            ab_test = AutomationABTest.query.get(test_id)
            if not ab_test:
                return None
            
            if variant == 'A':
                ab_test.variant_a_sent += sent
                ab_test.variant_a_opens += opens
                ab_test.variant_a_clicks += clicks
            else:
                ab_test.variant_b_sent += sent
                ab_test.variant_b_opens += opens
                ab_test.variant_b_clicks += clicks
            
            # Determine winner if enough data
            if ab_test.variant_a_sent >= 100 and ab_test.variant_b_sent >= 100:
                a_rate = (ab_test.variant_a_opens / ab_test.variant_a_sent * 100) if ab_test.variant_a_sent > 0 else 0
                b_rate = (ab_test.variant_b_opens / ab_test.variant_b_sent * 100) if ab_test.variant_b_sent > 0 else 0
                
                ab_test.winner_variant = 'A' if a_rate > b_rate else 'B'
                ab_test.status = 'completed'
                ab_test.completed_at = datetime.utcnow()
            
            db.session.commit()
            return ab_test
        except Exception as e:
            logger.error(f"Error updating A/B test results: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def update_trigger_template(trigger_id, name=None, description=None, trigger_type=None, 
                                  category=None, trigger_config=None, steps_template=None):
        """Update an existing trigger template"""
        try:
            trigger = AutomationTriggerLibrary.query.get(trigger_id)
            if not trigger:
                return None
            
            if name is not None:
                trigger.name = name
            if description is not None:
                trigger.description = description
            if trigger_type is not None:
                trigger.trigger_type = trigger_type
            if category is not None:
                trigger.category = category
            if trigger_config is not None:
                trigger.trigger_config = trigger_config
            if steps_template is not None:
                trigger.steps_template = steps_template
            
            db.session.commit()
            return trigger
        except Exception as e:
            logger.error(f"Error updating trigger template: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def duplicate_trigger_template(trigger_id, new_name=None):
        """Duplicate a trigger template"""
        import copy
        try:
            original = AutomationTriggerLibrary.query.get(trigger_id)
            if not original:
                return None
            
            # Use deep copy for JSON fields to avoid reference issues
            trigger_config = copy.deepcopy(dict(original.trigger_config)) if original.trigger_config else {}
            steps_template = copy.deepcopy(list(original.steps_template)) if original.steps_template else []
            
            duplicate = AutomationTriggerLibrary(
                name=new_name or f"{original.name} (Copy)",
                trigger_type=original.trigger_type,
                description=original.description,
                category=original.category,
                trigger_config=trigger_config,
                steps_template=steps_template,
                is_predefined=False,
                usage_count=0
            )
            db.session.add(duplicate)
            db.session.commit()
            return duplicate
        except Exception as e:
            logger.error(f"Error duplicating trigger template: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def delete_trigger_template(trigger_id):
        """Delete a trigger template (only non-predefined)"""
        try:
            trigger = AutomationTriggerLibrary.query.get(trigger_id)
            if not trigger:
                return False
            
            db.session.delete(trigger)
            db.session.commit()
            return True
        except Exception as e:
            logger.error(f"Error deleting trigger template: {e}")
            db.session.rollback()
            return False
    
    @staticmethod
    def seed_trigger_library():
        """Seed database with comprehensive pre-built triggers"""
        templates = [
            # ===== ENGAGEMENT TRIGGERS =====
            {
                'name': 'Welcome Series',
                'trigger_type': 'signup',
                'description': 'Send a 3-part welcome series to new subscribers introducing your brand, values, and products.',
                'category': 'engagement',
                'trigger_config': {
                    'event': 'user_signup',
                    'conditions': {'source': 'any'},
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Welcome Email', 'subject': 'Welcome to {brand}!', 'description': 'Immediate welcome with brand introduction'},
                    {'type': 'wait', 'delay': 24, 'description': 'Wait 24 hours for next message'},
                    {'type': 'email', 'delay': 0, 'template': 'Getting Started Guide', 'subject': 'Here\'s how to get started', 'description': 'Product/service introduction'},
                    {'type': 'wait', 'delay': 48, 'description': 'Wait 48 hours'},
                    {'type': 'email', 'delay': 0, 'template': 'First Purchase Offer', 'subject': 'A special gift for you', 'description': 'First-time buyer discount'}
                ]
            },
            {
                'name': 'Lead Magnet Delivery',
                'trigger_type': 'form_submit',
                'description': 'Deliver lead magnets (ebooks, guides, checklists) and nurture new leads with follow-up content.',
                'category': 'engagement',
                'trigger_config': {
                    'event': 'lead_magnet_download',
                    'form_type': 'lead_magnet',
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Lead Magnet Delivery', 'subject': 'Your download is ready!', 'description': 'Deliver the promised resource'},
                    {'type': 'wait', 'delay': 48, 'description': 'Wait 2 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Related Content', 'subject': 'You might also like...', 'description': 'Share related resources'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Consultation Offer', 'subject': 'Let\'s chat about your goals', 'description': 'Offer consultation or demo'}
                ]
            },
            {
                'name': 'Webinar Registration',
                'trigger_type': 'event_register',
                'description': 'Confirm webinar registration and send reminders leading up to the event.',
                'category': 'engagement',
                'trigger_config': {
                    'event': 'webinar_registration',
                    'event_type': 'webinar',
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Registration Confirmation', 'subject': 'You\'re registered!', 'description': 'Confirm registration with calendar invite'},
                    {'type': 'wait', 'delay': 24, 'description': 'Wait until 24 hours before event'},
                    {'type': 'email', 'delay': 0, 'template': '24 Hour Reminder', 'subject': 'Webinar tomorrow!', 'description': 'Reminder with prep materials'},
                    {'type': 'sms', 'delay': 0, 'template': '1 Hour Reminder', 'message': 'Your webinar starts in 1 hour! Join here: {link}', 'description': 'SMS reminder 1 hour before'}
                ]
            },
            {
                'name': 'Newsletter Subscriber Onboarding',
                'trigger_type': 'newsletter_signup',
                'description': 'Onboard new newsletter subscribers with your best content and set expectations.',
                'category': 'engagement',
                'trigger_config': {
                    'event': 'newsletter_subscribe',
                    'list_type': 'newsletter',
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Newsletter Welcome', 'subject': 'Welcome to our newsletter!', 'description': 'Set expectations and share top content'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Best Of Collection', 'subject': 'Our most popular articles', 'description': 'Share curated best content'}
                ]
            },
            
            # ===== ECOMMERCE TRIGGERS =====
            {
                'name': 'Abandoned Cart Recovery',
                'trigger_type': 'abandoned_cart',
                'description': 'Multi-touch campaign to recover abandoned shopping carts with urgency and incentives.',
                'category': 'ecommerce',
                'trigger_config': {
                    'event': 'cart_abandoned',
                    'wait_hours': 1,
                    'min_cart_value': 0,
                    'timing': 'delayed'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 1, 'template': 'Cart Reminder', 'subject': 'You left something behind!', 'description': 'Friendly reminder with cart contents'},
                    {'type': 'wait', 'delay': 24, 'description': 'Wait 24 hours'},
                    {'type': 'email', 'delay': 0, 'template': 'Social Proof', 'subject': 'Others love this too', 'description': 'Show reviews and testimonials'},
                    {'type': 'wait', 'delay': 48, 'description': 'Wait 48 hours'},
                    {'type': 'email', 'delay': 0, 'template': 'Final Discount', 'subject': '10% off - just for you', 'description': 'Last chance with discount code'},
                    {'type': 'sms', 'delay': 0, 'template': 'SMS Reminder', 'message': 'Your cart is waiting! Complete your order: {link}', 'description': 'SMS nudge for mobile users'}
                ]
            },
            {
                'name': 'Browse Abandonment',
                'trigger_type': 'browse_abandon',
                'description': 'Re-engage visitors who browsed products but didn\'t add to cart.',
                'category': 'ecommerce',
                'trigger_config': {
                    'event': 'browse_abandon',
                    'min_pages_viewed': 3,
                    'wait_hours': 2,
                    'timing': 'delayed'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 2, 'template': 'Product Recommendations', 'subject': 'Based on what you viewed...', 'description': 'Show recently viewed items'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Category Highlights', 'subject': 'Popular in {category}', 'description': 'Showcase category bestsellers'}
                ]
            },
            {
                'name': 'Post-Purchase Thank You',
                'trigger_type': 'purchase_complete',
                'description': 'Thank customers after purchase and set expectations for delivery.',
                'category': 'ecommerce',
                'trigger_config': {
                    'event': 'order_placed',
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Order Confirmation', 'subject': 'Thank you for your order!', 'description': 'Order details and next steps'},
                    {'type': 'wait', 'delay': 24, 'description': 'Wait 24 hours'},
                    {'type': 'email', 'delay': 0, 'template': 'Shipping Update', 'subject': 'Your order has shipped!', 'description': 'Tracking information'},
                    {'type': 'wait', 'delay': 168, 'description': 'Wait 7 days after delivery'},
                    {'type': 'email', 'delay': 0, 'template': 'Review Request', 'subject': 'How did we do?', 'description': 'Request product review'}
                ]
            },
            {
                'name': 'Cross-Sell Campaign',
                'trigger_type': 'purchase_complete',
                'description': 'Recommend complementary products after a purchase.',
                'category': 'ecommerce',
                'trigger_config': {
                    'event': 'order_delivered',
                    'wait_days': 7,
                    'timing': 'delayed'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 168, 'template': 'Complementary Products', 'subject': 'Complete your collection', 'description': 'Show related products'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Bundle Offer', 'subject': 'Save when you bundle', 'description': 'Offer product bundles with discount'}
                ]
            },
            {
                'name': 'Replenishment Reminder',
                'trigger_type': 'product_lifecycle',
                'description': 'Remind customers to reorder consumable products before they run out.',
                'category': 'ecommerce',
                'trigger_config': {
                    'event': 'replenishment_due',
                    'product_type': 'consumable',
                    'days_before_empty': 7,
                    'timing': 'scheduled'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Reorder Reminder', 'subject': 'Time to restock?', 'description': 'Reminder based on usage patterns'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'sms', 'delay': 0, 'template': 'SMS Reorder', 'message': 'Running low? Reorder now: {link}', 'description': 'SMS follow-up'},
                    {'type': 'wait', 'delay': 48, 'description': 'Wait 2 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Subscription Offer', 'subject': 'Never run out again', 'description': 'Offer subscription option'}
                ]
            },
            {
                'name': 'Flash Sale Announcement',
                'trigger_type': 'scheduled_event',
                'description': 'Announce flash sales with countdown urgency and exclusive access.',
                'category': 'ecommerce',
                'trigger_config': {
                    'event': 'flash_sale_start',
                    'sale_duration_hours': 24,
                    'timing': 'scheduled'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Sale Announcement', 'subject': 'FLASH SALE: 24 hours only!', 'description': 'Announce sale with top deals'},
                    {'type': 'sms', 'delay': 0, 'template': 'SMS Alert', 'message': 'Flash sale NOW! Up to 50% off. Shop: {link}', 'description': 'Immediate SMS for engagement'},
                    {'type': 'wait', 'delay': 12, 'description': 'Wait 12 hours'},
                    {'type': 'email', 'delay': 0, 'template': 'Last Chance', 'subject': 'Only 12 hours left!', 'description': 'Urgency reminder'},
                    {'type': 'social', 'delay': 0, 'platform': 'all', 'template': 'Social Reminder', 'description': 'Post sale reminder on social media'}
                ]
            },
            
            # ===== NURTURE TRIGGERS =====
            {
                'name': 'Educational Drip Campaign',
                'trigger_type': 'tag_added',
                'description': 'Educate leads about your industry with valuable content over time.',
                'category': 'nurture',
                'trigger_config': {
                    'event': 'tag_applied',
                    'tag': 'needs_education',
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Industry Basics', 'subject': 'Getting started with {topic}', 'description': 'Foundational content'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Intermediate Guide', 'subject': 'Taking it to the next level', 'description': 'More advanced concepts'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Advanced Strategies', 'subject': 'Pro tips from experts', 'description': 'Expert-level content'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Case Study', 'subject': 'See how {company} succeeded', 'description': 'Real-world success story'},
                    {'type': 'wait', 'delay': 48, 'description': 'Wait 2 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Consultation CTA', 'subject': 'Ready to apply this?', 'description': 'Offer consultation'}
                ]
            },
            {
                'name': 'Product Demo Follow-up',
                'trigger_type': 'demo_completed',
                'description': 'Nurture leads after a product demo with resources and buying guides.',
                'category': 'nurture',
                'trigger_config': {
                    'event': 'demo_attended',
                    'demo_type': 'product',
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 1, 'template': 'Demo Recap', 'subject': 'Thanks for joining our demo!', 'description': 'Summary and recording link'},
                    {'type': 'wait', 'delay': 24, 'description': 'Wait 24 hours'},
                    {'type': 'email', 'delay': 0, 'template': 'FAQ & Objections', 'subject': 'Your questions answered', 'description': 'Address common concerns'},
                    {'type': 'wait', 'delay': 48, 'description': 'Wait 48 hours'},
                    {'type': 'email', 'delay': 0, 'template': 'Pricing & Plans', 'subject': 'Finding the right plan for you', 'description': 'Pricing breakdown'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Special Offer', 'subject': 'Limited time offer', 'description': 'Incentive to convert'}
                ]
            },
            {
                'name': 'Free Trial Conversion',
                'trigger_type': 'trial_started',
                'description': 'Guide free trial users to become paying customers.',
                'category': 'nurture',
                'trigger_config': {
                    'event': 'trial_start',
                    'trial_length_days': 14,
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Trial Welcome', 'subject': 'Your trial has started!', 'description': 'Quick start guide'},
                    {'type': 'wait', 'delay': 24, 'description': 'Wait 1 day'},
                    {'type': 'email', 'delay': 0, 'template': 'Key Features', 'subject': 'Don\'t miss these features', 'description': 'Highlight top features'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Success Stories', 'subject': 'How others are winning', 'description': 'Customer success examples'},
                    {'type': 'wait', 'delay': 168, 'description': 'Wait until day 10'},
                    {'type': 'email', 'delay': 0, 'template': 'Trial Ending Soon', 'subject': 'Your trial ends in 4 days', 'description': 'Urgency reminder'},
                    {'type': 'sms', 'delay': 0, 'template': 'SMS Reminder', 'message': 'Your trial ends soon! Upgrade now: {link}', 'description': 'SMS nudge'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait until day 13'},
                    {'type': 'email', 'delay': 0, 'template': 'Last Day Offer', 'subject': 'LAST DAY: Special upgrade offer', 'description': 'Final conversion push'}
                ]
            },
            {
                'name': 'Lead Scoring Trigger',
                'trigger_type': 'score_threshold',
                'description': 'Trigger sales outreach when a lead reaches a high engagement score.',
                'category': 'nurture',
                'trigger_config': {
                    'event': 'lead_score_reached',
                    'min_score': 50,
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'notification', 'delay': 0, 'template': 'Sales Alert', 'description': 'Notify sales team of hot lead'},
                    {'type': 'task', 'delay': 0, 'template': 'Create Follow-up Task', 'description': 'Assign sales rep to contact'},
                    {'type': 'email', 'delay': 0, 'template': 'Personal Outreach', 'subject': 'Quick question for you', 'description': 'Personal email from rep'}
                ]
            },
            
            # ===== RETENTION TRIGGERS =====
            {
                'name': 'Re-engagement Campaign',
                'trigger_type': 'inactive',
                'description': 'Win back subscribers who haven\'t engaged in 30+ days.',
                'category': 'retention',
                'trigger_config': {
                    'event': 'inactivity_detected',
                    'inactive_days': 30,
                    'timing': 'scheduled'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'We Miss You', 'subject': 'It\'s been a while...', 'description': 'Friendly check-in'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'What\'s New', 'subject': 'Look what you\'ve been missing', 'description': 'Highlight new features/products'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Special Offer', 'subject': 'Come back and save 20%', 'description': 'Incentive to return'},
                    {'type': 'wait', 'delay': 168, 'description': 'Wait 7 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Final Notice', 'subject': 'Should we keep in touch?', 'description': 'Last chance before removal'}
                ]
            },
            {
                'name': 'Customer Milestone Celebration',
                'trigger_type': 'milestone_reached',
                'description': 'Celebrate customer anniversaries and milestones.',
                'category': 'retention',
                'trigger_config': {
                    'event': 'customer_anniversary',
                    'milestone_types': ['signup', 'first_purchase', 'loyalty_tier'],
                    'timing': 'scheduled'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Anniversary Celebration', 'subject': 'Happy {years} year anniversary!', 'description': 'Celebrate with special offer'},
                    {'type': 'social', 'delay': 0, 'platform': 'all', 'template': 'Social Shoutout', 'description': 'Optional public recognition'}
                ]
            },
            {
                'name': 'Churn Prevention',
                'trigger_type': 'churn_risk',
                'description': 'Identify and save at-risk customers before they leave.',
                'category': 'retention',
                'trigger_config': {
                    'event': 'churn_risk_detected',
                    'risk_score_min': 70,
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'notification', 'delay': 0, 'template': 'Churn Alert', 'description': 'Notify customer success team'},
                    {'type': 'email', 'delay': 0, 'template': 'Check-in', 'subject': 'How can we help?', 'description': 'Personal outreach'},
                    {'type': 'wait', 'delay': 48, 'description': 'Wait 2 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Value Reminder', 'subject': 'Getting the most from {product}', 'description': 'Highlight unused features'},
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Special Retention Offer', 'subject': 'A special offer just for you', 'description': 'Discount or upgrade offer'}
                ]
            },
            {
                'name': 'VIP Customer Program',
                'trigger_type': 'spending_threshold',
                'description': 'Reward top customers with VIP perks and exclusive access.',
                'category': 'retention',
                'trigger_config': {
                    'event': 'lifetime_value_reached',
                    'min_ltv': 1000,
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'VIP Welcome', 'subject': 'You\'re now a VIP!', 'description': 'Welcome to exclusive tier'},
                    {'type': 'wait', 'delay': 24, 'description': 'Wait 1 day'},
                    {'type': 'email', 'delay': 0, 'template': 'VIP Benefits', 'subject': 'Your exclusive VIP perks', 'description': 'Detail all VIP benefits'},
                    {'type': 'sms', 'delay': 0, 'template': 'VIP SMS Club', 'message': 'Welcome to VIP! Reply YES for exclusive text deals.', 'description': 'Opt-in to SMS VIP club'}
                ]
            },
            {
                'name': 'Feedback & NPS Survey',
                'trigger_type': 'interaction_complete',
                'description': 'Collect customer feedback after purchases or support interactions.',
                'category': 'retention',
                'trigger_config': {
                    'event': 'interaction_completed',
                    'interaction_types': ['purchase', 'support_ticket', 'subscription_renewal'],
                    'timing': 'delayed'
                },
                'steps_template': [
                    {'type': 'wait', 'delay': 72, 'description': 'Wait 3 days after interaction'},
                    {'type': 'email', 'delay': 0, 'template': 'NPS Survey', 'subject': 'How likely are you to recommend us?', 'description': 'NPS survey request'},
                    {'type': 'condition', 'condition': 'nps_score < 7', 'description': 'Branch based on NPS score'},
                    {'type': 'notification', 'delay': 0, 'template': 'Detractor Alert', 'description': 'Alert team to follow up with detractors'}
                ]
            },
            
            # ===== SMS TRIGGERS =====
            {
                'name': 'SMS Welcome',
                'trigger_type': 'sms_optin',
                'description': 'Welcome new SMS subscribers with an immediate discount code.',
                'category': 'sms',
                'trigger_config': {
                    'event': 'sms_subscribe',
                    'list_type': 'marketing',
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'sms', 'delay': 0, 'template': 'SMS Welcome', 'message': 'Welcome to {brand}! Use code WELCOME10 for 10% off. Reply STOP to unsubscribe.', 'description': 'Immediate welcome with offer'}
                ]
            },
            {
                'name': 'Appointment Reminder',
                'trigger_type': 'appointment_scheduled',
                'description': 'Send SMS reminders before scheduled appointments.',
                'category': 'sms',
                'trigger_config': {
                    'event': 'appointment_created',
                    'reminder_times': [24, 2],
                    'timing': 'scheduled'
                },
                'steps_template': [
                    {'type': 'sms', 'delay': -1440, 'template': '24h Reminder', 'message': 'Reminder: Your appointment is tomorrow at {time}. Reply C to confirm.', 'description': '24 hours before'},
                    {'type': 'sms', 'delay': -120, 'template': '2h Reminder', 'message': 'Your appointment is in 2 hours at {location}. See you soon!', 'description': '2 hours before'}
                ]
            },
            {
                'name': 'Order Status Updates',
                'trigger_type': 'order_status_change',
                'description': 'Keep customers informed with SMS order updates.',
                'category': 'sms',
                'trigger_config': {
                    'event': 'order_status_updated',
                    'statuses': ['shipped', 'out_for_delivery', 'delivered'],
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'condition', 'condition': 'status == shipped', 'description': 'Check order status'},
                    {'type': 'sms', 'delay': 0, 'template': 'Shipped', 'message': 'Your order has shipped! Track it here: {tracking_link}', 'description': 'Shipping notification'},
                    {'type': 'condition', 'condition': 'status == delivered', 'description': 'Check if delivered'},
                    {'type': 'sms', 'delay': 0, 'template': 'Delivered', 'message': 'Your order has been delivered! Enjoy your purchase.', 'description': 'Delivery confirmation'}
                ]
            },
            
            # ===== SOCIAL TRIGGERS =====
            {
                'name': 'New Follower Welcome',
                'trigger_type': 'social_follow',
                'description': 'Welcome new social media followers with a special offer.',
                'category': 'social',
                'trigger_config': {
                    'event': 'new_follower',
                    'platforms': ['instagram', 'tiktok', 'twitter'],
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'social_dm', 'delay': 0, 'template': 'Welcome DM', 'message': 'Thanks for following! Here\'s 10% off: SOCIAL10', 'description': 'DM with offer'},
                    {'type': 'wait', 'delay': 168, 'description': 'Wait 7 days'},
                    {'type': 'email', 'delay': 0, 'template': 'Cross-Channel Connect', 'subject': 'Stay connected everywhere', 'description': 'Invite to other channels'}
                ]
            },
            {
                'name': 'UGC Collection',
                'trigger_type': 'hashtag_mention',
                'description': 'Collect and thank users for user-generated content.',
                'category': 'social',
                'trigger_config': {
                    'event': 'brand_hashtag_used',
                    'hashtags': ['{brand}', '{brand}community'],
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'social_comment', 'delay': 0, 'template': 'Thank You Comment', 'message': 'Love this! Thanks for sharing!', 'description': 'Comment on post'},
                    {'type': 'notification', 'delay': 0, 'template': 'UGC Alert', 'description': 'Notify team of potential content'},
                    {'type': 'email', 'delay': 24, 'template': 'Feature Request', 'subject': 'Can we feature your content?', 'description': 'Ask permission to repost'}
                ]
            },
            {
                'name': 'Social Contest Entry',
                'trigger_type': 'contest_entry',
                'description': 'Manage social media contest entries and announcements.',
                'category': 'social',
                'trigger_config': {
                    'event': 'contest_entry_received',
                    'contest_type': 'social',
                    'timing': 'immediate'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Entry Confirmation', 'subject': 'You\'re entered to win!', 'description': 'Confirm contest entry'},
                    {'type': 'social', 'delay': 0, 'platform': 'all', 'template': 'Contest Update', 'description': 'Post entry count updates'},
                    {'type': 'wait', 'delay': 0, 'description': 'Wait until contest end date'},
                    {'type': 'email', 'delay': 0, 'template': 'Winner Announcement', 'subject': 'The winner is...', 'description': 'Announce winner(s)'}
                ]
            },
            
            # ===== BIRTHDAY/HOLIDAY TRIGGERS =====
            {
                'name': 'Birthday Campaign',
                'trigger_type': 'birthday',
                'description': 'Send personalized birthday greetings with a special gift.',
                'category': 'engagement',
                'trigger_config': {
                    'event': 'customer_birthday',
                    'days_before': 0,
                    'timing': 'scheduled'
                },
                'steps_template': [
                    {'type': 'email', 'delay': 0, 'template': 'Birthday Greeting', 'subject': 'Happy Birthday! A gift for you', 'description': 'Birthday wish with discount'},
                    {'type': 'sms', 'delay': 0, 'template': 'Birthday SMS', 'message': 'Happy Birthday! Enjoy 25% off with code BDAY25!', 'description': 'SMS birthday offer'}
                ]
            },
            {
                'name': 'Holiday Campaign',
                'trigger_type': 'scheduled_holiday',
                'description': 'Run holiday-themed campaigns with seasonal offers.',
                'category': 'engagement',
                'trigger_config': {
                    'event': 'holiday_trigger',
                    'holidays': ['christmas', 'black_friday', 'new_year', 'valentines'],
                    'days_before': 7,
                    'timing': 'scheduled'
                },
                'steps_template': [
                    {'type': 'email', 'delay': -168, 'template': 'Holiday Preview', 'subject': '{holiday} is coming!', 'description': 'Tease upcoming offers'},
                    {'type': 'social', 'delay': -168, 'platform': 'all', 'template': 'Holiday Countdown', 'description': 'Social countdown posts'},
                    {'type': 'email', 'delay': 0, 'template': 'Holiday Sale', 'subject': '{holiday} Sale is LIVE!', 'description': 'Main holiday offer'},
                    {'type': 'sms', 'delay': 0, 'template': 'Holiday SMS', 'message': '{holiday} sale is here! Shop now: {link}', 'description': 'SMS blast'},
                    {'type': 'wait', 'delay': 24, 'description': 'Wait 24 hours'},
                    {'type': 'email', 'delay': 0, 'template': 'Last Chance', 'subject': 'Last day for {holiday} savings!', 'description': 'Urgency reminder'}
                ]
            }
        ]
        
        for template in templates:
            existing = AutomationTriggerLibrary.query.filter_by(name=template['name']).first()
            if not existing:
                AutomationService.create_trigger_template(**template)
