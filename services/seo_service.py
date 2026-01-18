"""
SEO Service - Phase 2
Handles keyword tracking, backlink monitoring, competitor analysis, and site audits
"""

from datetime import datetime
from extensions import db
from models import (SEOKeyword, KeywordRanking, SEOBacklink, SEOCompetitor, 
                    CompetitorSnapshot, SEOAudit, SEOPage)
import logging

logger = logging.getLogger(__name__)

class SEOService:
    @staticmethod
    def track_keyword(keyword, target_url=None, search_engine='google', location='US'):
        """Add keyword to tracking"""
        try:
            kw = SEOKeyword(
                keyword=keyword,
                target_url=target_url,
                search_engine=search_engine,
                location=location
            )
            db.session.add(kw)
            db.session.commit()
            return kw
        except Exception as e:
            logger.error(f"Error tracking keyword: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def update_keyword_position(keyword_id, position, url=None, impressions=0, clicks=0):
        """Update keyword ranking"""
        try:
            keyword = SEOKeyword.query.get(keyword_id)
            if keyword:
                keyword.previous_position = keyword.current_position
                keyword.current_position = position
                if not keyword.best_position or position < keyword.best_position:
                    keyword.best_position = position
                keyword.last_checked = datetime.utcnow()
                
                # Save historical ranking
                ranking = KeywordRanking(
                    keyword_id=keyword_id,
                    position=position,
                    url=url,
                    impressions=impressions,
                    clicks=clicks,
                    ctr=(clicks / impressions * 100) if impressions > 0 else 0
                )
                db.session.add(ranking)
                db.session.commit()
                return keyword
            return None
        except Exception as e:
            logger.error(f"Error updating keyword position: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def add_backlink(source_url, target_url, anchor_text=None, domain_authority=0):
        """Track a new backlink"""
        try:
            backlink = SEOBacklink(
                source_url=source_url,
                source_domain=source_url.split('/')[2] if '//' in source_url else source_url,
                target_url=target_url,
                anchor_text=anchor_text,
                domain_authority=domain_authority
            )
            db.session.add(backlink)
            db.session.commit()
            return backlink
        except Exception as e:
            logger.error(f"Error adding backlink: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def add_competitor(name, domain):
        """Add competitor for tracking"""
        try:
            competitor = SEOCompetitor(name=name, domain=domain)
            db.session.add(competitor)
            db.session.commit()
            return competitor
        except Exception as e:
            logger.error(f"Error adding competitor: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def update_competitor_metrics(competitor_id, metrics):
        """Update competitor metrics and save snapshot"""
        try:
            competitor = SEOCompetitor.query.get(competitor_id)
            if competitor:
                competitor.organic_traffic = metrics.get('organic_traffic', 0)
                competitor.organic_keywords = metrics.get('organic_keywords', 0)
                competitor.backlinks = metrics.get('backlinks', 0)
                competitor.domain_authority = metrics.get('domain_authority', 0)
                competitor.last_analyzed = datetime.utcnow()
                
                # Save snapshot
                snapshot = CompetitorSnapshot(
                    competitor_id=competitor_id,
                    organic_traffic=metrics.get('organic_traffic', 0),
                    organic_keywords=metrics.get('organic_keywords', 0),
                    backlinks=metrics.get('backlinks', 0),
                    domain_authority=metrics.get('domain_authority', 0),
                    top_keywords=metrics.get('top_keywords', [])
                )
                db.session.add(snapshot)
                db.session.commit()
                return competitor
            return None
        except Exception as e:
            logger.error(f"Error updating competitor metrics: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def run_site_audit(url, audit_type='full'):
        """Run comprehensive site audit"""
        try:
            audit = SEOAudit(
                url=url,
                audit_type=audit_type,
                status='running',
                started_at=datetime.utcnow()
            )
            db.session.add(audit)
            db.session.commit()
            
            # Simulate audit (in production, integrate with real SEO tools)
            audit.overall_score = 75
            audit.technical_score = 80
            audit.content_score = 70
            audit.performance_score = 85
            audit.mobile_score = 90
            audit.issues_found = [
                {'severity': 'medium', 'issue': 'Missing meta descriptions on 3 pages'},
                {'severity': 'low', 'issue': 'Some images missing alt text'}
            ]
            audit.recommendations = [
                'Add meta descriptions to all pages',
                'Optimize images with proper alt attributes',
                'Improve page load speed'
            ]
            audit.status = 'completed'
            audit.completed_at = datetime.utcnow()
            db.session.commit()
            
            return audit
        except Exception as e:
            logger.error(f"Error running site audit: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def track_page(url, title=None, meta_description=None, word_count=0):
        """Track individual page metrics"""
        try:
            page = SEOPage.query.filter_by(url=url).first()
            if not page:
                page = SEOPage(url=url)
            
            page.title = title
            page.meta_description = meta_description
            page.word_count = word_count
            page.last_crawled = datetime.utcnow()
            
            db.session.add(page)
            db.session.commit()
            return page
        except Exception as e:
            logger.error(f"Error tracking page: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def get_dashboard_stats():
        """Get SEO dashboard statistics"""
        try:
            total_keywords = SEOKeyword.query.filter_by(is_tracking=True).count()
            total_backlinks = SEOBacklink.query.filter_by(status='active').count()
            total_competitors = SEOCompetitor.query.filter_by(is_active=True).count()
            
            # Top performing keywords (position 1-10)
            top_keywords = SEOKeyword.query.filter(
                SEOKeyword.current_position.isnot(None),
                SEOKeyword.current_position <= 10
            ).count()
            
            return {
                'total_keywords': total_keywords,
                'top_performing': top_keywords,
                'total_backlinks': total_backlinks,
                'total_competitors': total_competitors
            }
        except Exception as e:
            logger.error(f"Error getting dashboard stats: {e}")
            return {}
