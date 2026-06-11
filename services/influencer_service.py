"""
Influencer CRM Service
Manage influencer relationships and campaigns
"""

from datetime import datetime, timedelta
from models import db
import logging

logger = logging.getLogger(__name__)


class InfluencerService:
    """Service for influencer relationship management"""
    
    @staticmethod
    def create_influencer_profile(data):
        """
        Create influencer profile.
        
        Args:
            data: Influencer information
        
        Returns:
            dict: Created profile
        """
        try:
            from models import Influencer
            
            influencer = Influencer(
                name=data.get('name'),
                email=data.get('email'),
                instagram_handle=data.get('instagram'),
                tiktok_handle=data.get('tiktok'),
                youtube_channel=data.get('youtube'),
                twitter_handle=data.get('twitter'),
                niche=data.get('niche'),
                follower_count=data.get('followers', 0),
                engagement_rate=data.get('engagement_rate', 0),
                tier=data.get('tier', 'micro'),  # nano, micro, mid, macro, mega
                notes=data.get('notes'),
                status='prospect'
            )
            
            db.session.add(influencer)
            db.session.commit()
            
            return {
                'success': True,
                'influencer_id': influencer.id,
                'name': influencer.name
            }
            
        except Exception as e:
            logger.error(f"Error creating influencer profile: {e}")
            db.session.rollback()
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def track_content_performance(content_id, metrics):
        """
        Track influencer content performance.
        
        Args:
            content_id: Content piece ID
            metrics: Performance metrics
        
        Returns:
            dict: Updated metrics
        """
        try:
            from models import InfluencerContent
            
            content = db.session.get(InfluencerContent, content_id)
            if not content:
                return {'success': False, 'error': 'Content not found'}
            
            content.views = metrics.get('views', 0)
            content.likes = metrics.get('likes', 0)
            content.comments = metrics.get('comments', 0)
            content.shares = metrics.get('shares', 0)
            content.clicks = metrics.get('clicks', 0)
            content.conversions = metrics.get('conversions', 0)
            content.last_updated = datetime.utcnow()
            
            # Calculate engagement rate
            if content.views > 0:
                engagement = (content.likes + content.comments + content.shares) / content.views * 100
                content.engagement_rate = engagement
            
            db.session.commit()
            
            return {
                'success': True,
                'content_id': content_id,
                'engagement_rate': content.engagement_rate
            }
            
        except Exception as e:
            logger.error(f"Error tracking content performance: {e}")
            db.session.rollback()
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def get_influencer_stats(influencer_id):
        """
        Get comprehensive influencer statistics.
        
        Returns:
            dict: Influencer performance data
        """
        try:
            from models import Influencer, InfluencerContent
            from sqlalchemy import func
            
            influencer = db.session.get(Influencer, influencer_id)
            if not influencer:
                return {'success': False, 'error': 'Influencer not found'}
            
            # Get content stats
            content_stats = db.session.query(
                func.count(InfluencerContent.id).label('total_posts'),
                func.sum(InfluencerContent.views).label('total_views'),
                func.sum(InfluencerContent.likes).label('total_likes'),
                func.sum(InfluencerContent.clicks).label('total_clicks'),
                func.sum(InfluencerContent.conversions).label('total_conversions'),
                func.avg(InfluencerContent.engagement_rate).label('avg_engagement')
            ).filter(
                InfluencerContent.influencer_id == influencer_id
            ).first()
            
            return {
                'success': True,
                'influencer': {
                    'id': influencer.id,
                    'name': influencer.name,
                    'tier': influencer.tier,
                    'follower_count': influencer.follower_count,
                    'status': influencer.status
                },
                'content': {
                    'total_posts': content_stats.total_posts or 0,
                    'total_views': content_stats.total_views or 0,
                    'total_likes': content_stats.total_likes or 0,
                    'total_clicks': content_stats.total_clicks or 0,
                    'total_conversions': content_stats.total_conversions or 0,
                    'avg_engagement': content_stats.avg_engagement or 0
                }
            }
            
        except Exception as e:
            logger.error(f"Error getting influencer stats: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def generate_brief(campaign_data):
        """
        Generate influencer campaign brief.
        
        Args:
            campaign_data: Campaign requirements
        
        Returns:
            dict: Generated brief
        """
        brief = {
            'campaign_name': campaign_data.get('name'),
            'brand': campaign_data.get('brand'),
            'objective': campaign_data.get('objective'),
            'target_audience': campaign_data.get('audience'),
            'key_messages': campaign_data.get('messages', []),
            'deliverables': campaign_data.get('deliverables', []),
            'timeline': campaign_data.get('timeline'),
            'budget': campaign_data.get('budget'),
            'content_guidelines': {
                'tone': campaign_data.get('tone', 'authentic'),
                'hashtags': campaign_data.get('hashtags', []),
                'mentions': campaign_data.get('mentions', []),
                'do_not': campaign_data.get('restrictions', [])
            },
            'success_metrics': {
                'views_target': campaign_data.get('view_target'),
                'engagement_target': campaign_data.get('engagement_target'),
                'conversion_target': campaign_data.get('conversion_target')
            },
            'compensation': {
                'type': campaign_data.get('compensation_type'),
                'amount': campaign_data.get('amount'),
                'payment_terms': campaign_data.get('payment_terms')
            }
        }
        
        return brief
    
    @staticmethod
    def search_influencers(filters):
        """
        Search influencers by criteria.
        
        Args:
            filters: Search filters
        
        Returns:
            list: Matching influencers
        """
        try:
            from models import Influencer
            
            query = Influencer.query
            
            if filters.get('niche'):
                query = query.filter(Influencer.niche == filters['niche'])
            
            if filters.get('tier'):
                query = query.filter(Influencer.tier == filters['tier'])
            
            if filters.get('min_followers'):
                query = query.filter(Influencer.follower_count >= filters['min_followers'])
            
            if filters.get('min_engagement'):
                query = query.filter(Influencer.engagement_rate >= filters['min_engagement'])
            
            if filters.get('status'):
                query = query.filter(Influencer.status == filters['status'])
            
            influencers = query.all()
            
            return [{
                'id': i.id,
                'name': i.name,
                'tier': i.tier,
                'niche': i.niche,
                'followers': i.follower_count,
                'engagement_rate': i.engagement_rate,
                'status': i.status
            } for i in influencers]
            
        except Exception as e:
            logger.error(f"Error searching influencers: {e}")
            return []
