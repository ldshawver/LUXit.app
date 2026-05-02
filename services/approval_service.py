"""
Approval Service - Centralized approval queue management
Handles all content approval workflows for manual and automated content
"""
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class ApprovalService:
    """Service for managing the unified approval queue"""
    
    @staticmethod
    def submit_for_approval(
        company_id: int,
        content_type: str,
        content_id: Optional[int],
        title: str,
        content_full: Dict[str, Any],
        creation_mode: str = 'manual',
        created_by_agent: Optional[str] = None,
        created_by_user_id: Optional[int] = None,
        ai_rationale: Optional[str] = None,
        confidence_score: Optional[float] = None,
        target_platform: Optional[str] = None,
        target_audience: Optional[str] = None,
        scheduled_publish_at: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Submit content to the approval queue
        ALL content (manual or automated) must go through this
        
        Args:
            company_id: Company ID
            content_type: Type of content (social_post, email_campaign, etc.)
            content_id: ID of the related content record
            title: Title for display in queue
            content_full: Full content payload
            creation_mode: 'manual' or 'automated'
            created_by_agent: Agent type if automated
            created_by_user_id: User ID if manual
            ai_rationale: Why AI generated this (if automated)
            confidence_score: AI confidence level (0.0-1.0)
            target_platform: Target publishing platform
            target_audience: Target audience description
            scheduled_publish_at: When to publish (if scheduled)
            
        Returns:
            Created approval queue item as dict
        """
        from models import ApprovalQueue, ApprovalAuditLog, db
        
        try:
            content_preview = ApprovalService._generate_preview(content_full)
            
            risk_level = ApprovalService._assess_risk(
                content_full, 
                confidence_score,
                creation_mode
            )
            
            compliance_flags = ApprovalService._check_compliance(content_full, content_type)
            
            approval_item = ApprovalQueue(
                company_id=company_id,
                content_type=content_type,
                content_id=content_id,
                creation_mode=creation_mode,
                created_by_agent=created_by_agent,
                created_by_user_id=created_by_user_id,
                title=title,
                content_preview=content_preview,
                content_full=content_full,
                ai_rationale=ai_rationale,
                confidence_score=confidence_score,
                risk_level=risk_level,
                compliance_flags=compliance_flags,
                status='pending_review',
                target_platform=target_platform,
                target_audience=target_audience,
                scheduled_publish_at=scheduled_publish_at
            )
            
            db.session.add(approval_item)
            db.session.flush()
            
            audit_log = ApprovalAuditLog(
                company_id=company_id,
                approval_queue_id=approval_item.id,
                action='created',
                action_by_user_id=created_by_user_id,
                action_by_agent=created_by_agent,
                new_status='pending_review',
                notes=f"{'Automated' if creation_mode == 'automated' else 'Manual'} submission to approval queue"
            )
            db.session.add(audit_log)
            
            db.session.commit()
            
            logger.info(f"Content submitted for approval: {content_type} ID {approval_item.id}")
            
            return {
                'success': True,
                'approval_id': approval_item.id,
                'status': 'pending_review',
                'item': approval_item.to_dict()
            }
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error submitting for approval: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def get_pending_items(company_id: int, filters: Optional[Dict] = None) -> List[Dict]:
        """Get all pending approval items for a company"""
        from models import ApprovalQueue
        
        query = ApprovalQueue.query.filter_by(
            company_id=company_id,
            status='pending_review'
        )
        
        if filters:
            if filters.get('content_type'):
                query = query.filter_by(content_type=filters['content_type'])
            if filters.get('creation_mode'):
                query = query.filter_by(creation_mode=filters['creation_mode'])
            if filters.get('target_platform'):
                query = query.filter_by(target_platform=filters['target_platform'])
            if filters.get('risk_level'):
                query = query.filter_by(risk_level=filters['risk_level'])
        
        items = query.order_by(ApprovalQueue.created_at.desc()).all()
        return [item.to_dict() for item in items]
    
    @staticmethod
    def get_item(approval_id: int) -> Optional[Dict]:
        """Get a single approval queue item"""
        from models import ApprovalQueue
        
        item = ApprovalQueue.query.get(approval_id)
        return item.to_dict() if item else None
    
    @staticmethod
    def approve(
        approval_id: int,
        user_id: int,
        notes: Optional[str] = None,
        schedule_at: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Approve content for publishing
        
        Args:
            approval_id: ID of the approval queue item
            user_id: ID of the approving user
            notes: Optional approval notes
            schedule_at: Optional scheduled publish time
        """
        from models import ApprovalQueue, ApprovalAuditLog, db
        
        try:
            item = ApprovalQueue.query.get(approval_id)
            if not item:
                return {'success': False, 'error': 'Item not found'}
            
            if item.status != 'pending_review':
                return {'success': False, 'error': f'Item is not pending review (status: {item.status})'}
            
            previous_status = item.status
            
            if schedule_at:
                item.status = 'scheduled'
                item.scheduled_publish_at = schedule_at
            else:
                item.status = 'approved'
            
            item.reviewed_by_user_id = user_id
            item.reviewed_at = datetime.utcnow()
            item.review_notes = notes
            
            audit_log = ApprovalAuditLog(
                company_id=item.company_id,
                approval_queue_id=approval_id,
                action='approved',
                action_by_user_id=user_id,
                previous_status=previous_status,
                new_status=item.status,
                notes=notes
            )
            db.session.add(audit_log)
            
            db.session.commit()

            logger.info(f"Content approved: {item.content_type} ID {approval_id}")

            if item.status == 'approved':
                ApprovalService._dispatch_approved_content(item)

            return {
                'success': True,
                'status': item.status,
                'item': item.to_dict()
            }

        except Exception as e:
            db.session.rollback()
            logger.error(f"Error approving content: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def reject(
        approval_id: int,
        user_id: int,
        reason: str,
        request_regeneration: bool = False
    ) -> Dict[str, Any]:
        """
        Reject content
        
        Args:
            approval_id: ID of the approval queue item
            user_id: ID of the rejecting user
            reason: Reason for rejection
            request_regeneration: Whether to request AI to regenerate
        """
        from models import ApprovalQueue, ApprovalAuditLog, db
        
        try:
            item = ApprovalQueue.query.get(approval_id)
            if not item:
                return {'success': False, 'error': 'Item not found'}
            
            previous_status = item.status
            item.status = 'rejected'
            item.reviewed_by_user_id = user_id
            item.reviewed_at = datetime.utcnow()
            item.review_notes = reason
            
            audit_log = ApprovalAuditLog(
                company_id=item.company_id,
                approval_queue_id=approval_id,
                action='rejected',
                action_by_user_id=user_id,
                previous_status=previous_status,
                new_status='rejected',
                notes=reason,
                changes_made={'request_regeneration': request_regeneration}
            )
            db.session.add(audit_log)
            
            db.session.commit()

            logger.info(f"Content rejected: {item.content_type} ID {approval_id}")

            if item.created_by_agent:
                ApprovalService._store_rejection_lesson(item, reason)

            return {
                'success': True,
                'status': 'rejected',
                'request_regeneration': request_regeneration
            }

        except Exception as e:
            db.session.rollback()
            logger.error(f"Error rejecting content: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def edit_content(
        approval_id: int,
        user_id: int,
        updated_content: Dict[str, Any],
        edit_notes: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Edit content in the approval queue
        Creates a new version while preserving history
        """
        from models import ApprovalQueue, ApprovalAuditLog, db
        
        try:
            item = ApprovalQueue.query.get(approval_id)
            if not item:
                return {'success': False, 'error': 'Item not found'}
            
            previous_content = item.content_full
            
            edit_history = item.edit_history or []
            edit_history.append({
                'edited_by': user_id,
                'edited_at': datetime.utcnow().isoformat(),
                'previous_content': previous_content,
                'notes': edit_notes
            })
            
            item.content_full = updated_content
            item.content_preview = ApprovalService._generate_preview(updated_content)
            item.edit_history = edit_history
            item.version = (item.version or 1) + 1
            
            if updated_content.get('title'):
                item.title = updated_content['title']
            
            audit_log = ApprovalAuditLog(
                company_id=item.company_id,
                approval_queue_id=approval_id,
                action='edited',
                action_by_user_id=user_id,
                changes_made={
                    'previous': previous_content,
                    'new': updated_content
                },
                notes=edit_notes
            )
            db.session.add(audit_log)
            
            db.session.commit()
            
            logger.info(f"Content edited: {item.content_type} ID {approval_id}, version {item.version}")
            
            return {
                'success': True,
                'version': item.version,
                'item': item.to_dict()
            }
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error editing content: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def cancel(approval_id: int, user_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
        """Cancel an approval queue item"""
        from models import ApprovalQueue, ApprovalAuditLog, db
        
        try:
            item = ApprovalQueue.query.get(approval_id)
            if not item:
                return {'success': False, 'error': 'Item not found'}
            
            previous_status = item.status
            item.status = 'cancelled'
            
            audit_log = ApprovalAuditLog(
                company_id=item.company_id,
                approval_queue_id=approval_id,
                action='cancelled',
                action_by_user_id=user_id,
                previous_status=previous_status,
                new_status='cancelled',
                notes=reason
            )
            db.session.add(audit_log)
            
            db.session.commit()
            
            return {'success': True, 'status': 'cancelled'}
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error cancelling content: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def mark_published(approval_id: int) -> Dict[str, Any]:
        """Mark approved content as published"""
        from models import ApprovalQueue, ApprovalAuditLog, db
        
        try:
            item = ApprovalQueue.query.get(approval_id)
            if not item:
                return {'success': False, 'error': 'Item not found'}
            
            if item.status not in ['approved', 'scheduled']:
                return {'success': False, 'error': 'Content must be approved before publishing'}
            
            previous_status = item.status
            item.status = 'published'
            item.published_at = datetime.utcnow()
            
            audit_log = ApprovalAuditLog(
                company_id=item.company_id,
                approval_queue_id=approval_id,
                action='published',
                previous_status=previous_status,
                new_status='published'
            )
            db.session.add(audit_log)
            
            db.session.commit()
            
            return {'success': True, 'status': 'published'}
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error marking as published: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def get_audit_trail(approval_id: int) -> List[Dict]:
        """Get complete audit trail for an approval item"""
        from models import ApprovalAuditLog
        
        logs = ApprovalAuditLog.query.filter_by(
            approval_queue_id=approval_id
        ).order_by(ApprovalAuditLog.created_at.asc()).all()
        
        return [log.to_dict() for log in logs]
    
    @staticmethod
    def get_queue_stats(company_id: int) -> Dict[str, Any]:
        """Get approval queue statistics"""
        from models import ApprovalQueue, db
        from sqlalchemy import func
        
        stats = db.session.query(
            ApprovalQueue.status,
            func.count(ApprovalQueue.id)
        ).filter_by(
            company_id=company_id
        ).group_by(ApprovalQueue.status).all()
        
        by_type = db.session.query(
            ApprovalQueue.content_type,
            func.count(ApprovalQueue.id)
        ).filter_by(
            company_id=company_id,
            status='pending_review'
        ).group_by(ApprovalQueue.content_type).all()
        
        by_mode = db.session.query(
            ApprovalQueue.creation_mode,
            func.count(ApprovalQueue.id)
        ).filter_by(
            company_id=company_id,
            status='pending_review'
        ).group_by(ApprovalQueue.creation_mode).all()
        
        return {
            'by_status': {s[0]: s[1] for s in stats},
            'pending_by_type': {t[0]: t[1] for t in by_type},
            'pending_by_mode': {m[0]: m[1] for m in by_mode},
            'total_pending': sum(s[1] for s in stats if s[0] == 'pending_review')
        }
    
    @staticmethod
    def _generate_preview(content: Dict[str, Any]) -> str:
        """Generate a preview from content for quick review"""
        preview_fields = ['content', 'body', 'message', 'text', 'description', 'caption', 'subject']
        
        for field in preview_fields:
            if field in content and content[field]:
                text = str(content[field])
                return text[:500] + '...' if len(text) > 500 else text
        
        return str(content)[:500]
    
    @staticmethod
    def _assess_risk(content: Dict[str, Any], confidence_score: Optional[float], creation_mode: str) -> str:
        """Assess risk level of content"""
        risk_score = 0
        
        if creation_mode == 'automated':
            risk_score += 1
        
        if confidence_score and confidence_score < 0.7:
            risk_score += 2
        elif confidence_score and confidence_score < 0.8:
            risk_score += 1
        
        content_str = str(content).lower()
        high_risk_words = ['limited time', 'act now', 'guaranteed', 'free money', 'click here']
        for word in high_risk_words:
            if word in content_str:
                risk_score += 1
        
        if risk_score >= 4:
            return 'critical'
        elif risk_score >= 3:
            return 'high'
        elif risk_score >= 2:
            return 'medium'
        return 'low'
    
    @staticmethod
    def _check_compliance(content: Dict[str, Any], content_type: str) -> List[Dict[str, str]]:
        """Check content for compliance issues"""
        flags = []
        content_str = str(content).lower()
        
        if 'no purchase necessary' not in content_str and any(w in content_str for w in ['win', 'prize', 'giveaway', 'contest']):
            flags.append({
                'type': 'legal',
                'message': 'Contest/giveaway detected - may need legal disclaimers'
            })
        
        if any(w in content_str for w in ['cure', 'treatment', 'medical']):
            flags.append({
                'type': 'health',
                'message': 'Health claims detected - verify compliance with regulations'
            })
        
        if any(w in content_str for w in ['investment', 'returns', 'profit', 'guarantee']):
            flags.append({
                'type': 'financial',
                'message': 'Financial claims detected - verify compliance'
            })
        
        return flags


class FeatureToggleService:
    """Service for managing feature toggles"""
    
    @staticmethod
    def get_toggle(company_id: int, feature_key: str) -> Optional[Dict]:
        """Get a specific feature toggle"""
        from models import FeatureToggle
        
        toggle = FeatureToggle.query.filter_by(
            company_id=company_id,
            feature_key=feature_key
        ).first()
        
        return toggle.to_dict() if toggle else None
    
    @staticmethod
    def is_enabled(company_id: int, feature_key: str) -> bool:
        """Check if a feature is enabled"""
        from models import FeatureToggle
        
        toggle = FeatureToggle.query.filter_by(
            company_id=company_id,
            feature_key=feature_key
        ).first()
        
        if not toggle:
            return False
        
        if toggle.emergency_stop:
            return False
        
        return toggle.is_enabled
    
    @staticmethod
    def is_automation_allowed(company_id: int, feature_key: str) -> bool:
        """Check if automated creation is allowed for a feature"""
        from models import FeatureToggle
        
        toggle = FeatureToggle.query.filter_by(
            company_id=company_id,
            feature_key=feature_key
        ).first()
        
        if not toggle:
            return False
        
        if toggle.emergency_stop:
            return False
        
        return toggle.is_enabled and toggle.allow_automated_creation
    
    @staticmethod
    def requires_approval(company_id: int, feature_key: str) -> bool:
        """Check if content requires approval before publishing"""
        from models import FeatureToggle
        
        toggle = FeatureToggle.query.filter_by(
            company_id=company_id,
            feature_key=feature_key
        ).first()
        
        if not toggle:
            return True
        
        return toggle.require_approval
    
    @staticmethod
    def get_all_toggles(company_id: int, category: Optional[str] = None) -> List[Dict]:
        """Get all feature toggles for a company"""
        from models import FeatureToggle
        
        query = FeatureToggle.query.filter_by(company_id=company_id)
        
        if category:
            query = query.filter_by(feature_category=category)
        
        toggles = query.all()
        return [t.to_dict() for t in toggles]
    
    @staticmethod
    def update_toggle(company_id: int, feature_key: str, updates: Dict, user_id: int) -> Dict[str, Any]:
        """Update a feature toggle"""
        from models import FeatureToggle, db
        
        try:
            toggle = FeatureToggle.query.filter_by(
                company_id=company_id,
                feature_key=feature_key
            ).first()
            
            if not toggle:
                return {'success': False, 'error': 'Toggle not found'}
            
            allowed_fields = [
                'is_enabled', 'allow_automated_creation', 'require_approval',
                'confidence_threshold', 'daily_limit', 'budget_ceiling',
                'risk_tolerance', 'content_aggressiveness', 'brand_strictness',
                'schedule_frequency', 'active_hours_start', 'active_hours_end',
                'emergency_stop', 'platform_rules'
            ]
            
            for field in allowed_fields:
                if field in updates:
                    setattr(toggle, field, updates[field])
            
            toggle.last_modified_by = user_id
            
            db.session.commit()
            
            logger.info(f"Feature toggle updated: {feature_key} for company {company_id}")
            
            return {'success': True, 'toggle': toggle.to_dict()}
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error updating toggle: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def emergency_stop_all(company_id: int, user_id: int) -> Dict[str, Any]:
        """Emergency stop all automation for a company"""
        from models import FeatureToggle, db
        
        try:
            FeatureToggle.query.filter_by(
                company_id=company_id
            ).update({
                'emergency_stop': True,
                'last_modified_by': user_id
            })
            
            db.session.commit()
            
            logger.warning(f"EMERGENCY STOP activated for company {company_id} by user {user_id}")
            
            return {'success': True, 'message': 'All automation stopped'}
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error in emergency stop: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def resume_all(company_id: int, user_id: int) -> Dict[str, Any]:
        """Resume all automation (clear emergency stops)"""
        from models import FeatureToggle, db

        try:
            FeatureToggle.query.filter_by(
                company_id=company_id
            ).update({
                'emergency_stop': False,
                'last_modified_by': user_id
            })

            db.session.commit()

            logger.info(f"Emergency stop cleared for company {company_id} by user {user_id}")

            return {'success': True, 'message': 'Emergency stop cleared'}

        except Exception as e:
            db.session.rollback()
            logger.error(f"Error clearing emergency stop: {e}")
            return {'success': False, 'error': str(e)}

    @staticmethod
    def _dispatch_approved_content(item) -> None:
        """Auto-publish approved content by content_type.

        Dispatches:
          email_campaign → set Campaign.status = 'approved'
          social_post    → set SocialPost.status = 'approved'
          blog_post      → set BlogPost.status = 'approved'

        Only updates the status field; actual sending/publishing
        is handled by the respective campaign/post workflow.
        """
        from extensions import db

        content_type = getattr(item, 'content_type', None)
        content_id = getattr(item, 'content_id', None)

        if not content_id:
            return

        try:
            if content_type == 'email_campaign':
                from models import Campaign
                record = Campaign.query.get(content_id)
                if record and record.status not in ('sending', 'sent'):
                    record.status = 'approved'
                    db.session.commit()
                    logger.info("Campaign %s marked approved via approval dispatch", content_id)

            elif content_type == 'social_post':
                try:
                    from models import SocialPost
                    record = SocialPost.query.get(content_id)
                    if record:
                        record.status = 'approved'
                        db.session.commit()
                        logger.info("SocialPost %s marked approved via approval dispatch", content_id)
                except ImportError:
                    pass

            elif content_type == 'blog_post':
                try:
                    from models import BlogPost
                    record = BlogPost.query.get(content_id)
                    if record:
                        record.status = 'approved'
                        db.session.commit()
                        logger.info("BlogPost %s marked approved via approval dispatch", content_id)
                except ImportError:
                    pass

            elif content_type == 'sms_campaign':
                from models import Campaign
                record = Campaign.query.get(content_id)
                if record and record.status not in ('sending', 'sent'):
                    record.status = 'approved'
                    db.session.commit()
                    logger.info("SMS Campaign %s marked approved via approval dispatch", content_id)

        except Exception as exc:
            try:
                db.session.rollback()
            except Exception:
                pass
            logger.warning("_dispatch_approved_content failed for %s %s: %s", content_type, content_id, exc)

    @staticmethod
    def _store_rejection_lesson(item, reason: str) -> None:
        """Store rejection feedback so agent learns from it.

        Writes a memory note tied to the agent and company so future
        generation tasks can reference past rejections.
        """
        try:
            from models import AgentMemory, db
            note = AgentMemory(
                company_id=item.company_id,
                agent_type=item.created_by_agent,
                memory_type='rejection_lesson',
                content={
                    'approval_id': item.id,
                    'content_type': item.content_type,
                    'title': item.title,
                    'rejection_reason': reason,
                    'rejected_at': datetime.utcnow().isoformat(),
                    'content_preview': item.content_preview,
                },
                importance=0.8,
            )
            db.session.add(note)
            db.session.commit()
            logger.info("Rejection lesson stored for agent %s, approval %s", item.created_by_agent, item.id)
        except Exception as exc:
            logger.debug("_store_rejection_lesson skipped: %s", exc)
    
    @staticmethod
    def initialize_toggles(company_id: int):
        """Initialize default feature toggles for a new company"""
        from models import seed_feature_toggles
        seed_feature_toggles(company_id)
