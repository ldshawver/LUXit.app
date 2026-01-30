"""
AI Agent Scheduler
Manages automated execution of all marketing AI agents
"""
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from flask import current_app

logger = logging.getLogger(__name__)

# Track agent execution history
agent_execution_history = []
agent_health_status = {}


def run_agent(agent, app, task_data, task_runner):
    """Run agent tasks inside the Flask app context."""
    with app.app_context():
        logger.info(
            "Running scheduled agent task within Flask app context for %s",
            agent.agent_name,
        )
        return task_runner(agent, task_data)


class AgentScheduler:
    """Scheduler for automated agent tasks"""
    
    def __init__(self, app=None):
        self.scheduler = BackgroundScheduler()
        self.agents = {}
        self.app = app
        logger.info("Agent Scheduler initialized")

    def set_app(self, app):
        """Attach Flask app for context-aware job execution."""
        self.app = app
    
    def register_agent(self, agent_type: str, agent_instance):
        """Register an agent instance for scheduling"""
        self.agents[agent_type] = agent_instance
        logger.info(f"Registered agent: {agent_type}")
    
    def schedule_brand_strategy_agent(self):
        """Schedule Brand & Strategy Agent - Quarterly strategy updates"""
        if 'brand_strategy' not in self.agents:
            return
        
        agent = self.agents['brand_strategy']
        
        # Quarterly strategy generation - First day of each quarter
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(agent, {
                'task_type': 'quarterly_strategy',
                'quarter': f'Q{((datetime.now().month-1)//3)+1} {datetime.now().year}',
                'business_goals': ['Growth', 'Engagement', 'Revenue'],
                'budget': 'Standard'
            }),
            trigger=CronTrigger(month='1,4,7,10', day=1, hour=9),
            id='brand_quarterly_strategy',
            name='Brand Strategy - Quarterly Planning'
        )
        
        # Monthly market research - First Monday of each month
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(agent, {
                'task_type': 'market_research',
                'industry': 'General',
                'focus_areas': ['trends', 'opportunities']
            }),
            trigger=CronTrigger(day='1-7', day_of_week='mon', hour=10),
            id='brand_monthly_research',
            name='Brand Strategy - Monthly Research'
        )
        
        logger.info("Brand Strategy Agent scheduled")
    
    def schedule_content_seo_agent(self):
        """Schedule Content & SEO Agent - Weekly blog posts"""
        if 'content_seo' not in self.agents:
            return
        
        agent = self.agents['content_seo']
        
        # Weekly blog post generation - Every Monday
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(agent, {
                'task_type': 'blog_post',
                'topic': 'Industry insights and tips',
                'word_count': 1500,
                'tone': 'professional'
            }),
            trigger=CronTrigger(day_of_week='mon', hour=8),
            id='content_weekly_blog',
            name='Content - Weekly Blog Post'
        )
        
        # Monthly content calendar - Last Friday of each month
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(agent, {
                'task_type': 'content_calendar',
                'month': (datetime.now() + timedelta(days=30)).strftime('%B %Y'),
                'frequency': 'weekly'
            }),
            trigger=CronTrigger(day='22-28', day_of_week='fri', hour=14),
            id='content_monthly_calendar',
            name='Content - Monthly Calendar'
        )
        
        logger.info("Content & SEO Agent scheduled")
    
    def schedule_analytics_agent(self):
        """Schedule Analytics Agent - Real-time tracking and reports"""
        if 'analytics' not in self.agents:
            return
        
        agent = self.agents['analytics']
        
        # Weekly performance summary - Every Friday
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(agent, {
                'task_type': 'performance_summary',
                'period_days': 7
            }),
            trigger=CronTrigger(day_of_week='fri', hour=16),
            id='analytics_weekly_summary',
            name='Analytics - Weekly Summary'
        )
        
        # Monthly performance report - First day of month
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(agent, {
                'task_type': 'performance_summary',
                'period_days': 30
            }),
            trigger=CronTrigger(day=1, hour=9),
            id='analytics_monthly_report',
            name='Analytics - Monthly Report'
        )
        
        # Daily optimization recommendations - Every day
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(agent, {
                'task_type': 'optimization_recommendations',
                'focus_area': 'overall'
            }),
            trigger=CronTrigger(hour=7),
            id='analytics_daily_recommendations',
            name='Analytics - Daily Recommendations'
        )
        
        logger.info("Analytics Agent scheduled")
    
    def schedule_creative_agent(self):
        """Schedule Creative Agent - On-demand creative generation"""
        if 'creative_design' not in self.agents:
            return
        
        # Creative agent typically runs on-demand, not on schedule
        # But we can schedule weekly creative asset generation
        
        agent = self.agents['creative_design']
        
        # Weekly creative assets - Every Wednesday
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(agent, {
                'task_type': 'social_media_graphic',
                'message': 'Weekly inspiration',
                'platform': 'instagram',
                'theme': 'modern'
            }),
            trigger=CronTrigger(day_of_week='wed', hour=10),
            id='creative_weekly_assets',
            name='Creative - Weekly Assets'
        )
        
        logger.info("Creative Agent scheduled")
    
    def schedule_additional_agents(self):
        """Schedule remaining 6 agents"""
        # Advertising Agent - Weekly campaign checks
        if 'advertising' in self.agents:
            agent = self.agents['advertising']
            self.scheduler.add_job(
                func=lambda: self._run_agent_task_with_context(
                    agent,
                    {'task_type': 'campaign_strategy'}
                ),
                trigger=CronTrigger(day_of_week='wed', hour=11),
                id='advertising_weekly_strategy',
                name='Advertising - Weekly Strategy Review'
            )
            logger.info("Advertising Agent scheduled")
        
        # Social Media Agent - Daily posts
        if 'social_media' in self.agents:
            agent = self.agents['social_media']
            self.scheduler.add_job(
                func=lambda: self._run_agent_task_with_context(
                    agent,
                    {'task_type': 'daily_posts'}
                ),
                trigger=CronTrigger(hour=9),
                id='social_daily_posts',
                name='Social Media - Daily Posts'
            )
            logger.info("Social Media Agent scheduled")
        
        # Email CRM Agent - Weekly campaigns
        if 'email_crm' in self.agents:
            agent = self.agents['email_crm']
            self.scheduler.add_job(
                func=lambda: self._run_agent_task_with_context(
                    agent,
                    {'task_type': 'weekly_campaign'}
                ),
                trigger=CronTrigger(day_of_week='tue', hour=10),
                id='email_weekly_campaign',
                name='Email CRM - Weekly Campaign'
            )
            # Daily subscriber sync
            self.scheduler.add_job(
                func=lambda: self._run_agent_task_with_context(
                    agent,
                    {'task_type': 'subscriber_sync'}
                ),
                trigger=CronTrigger(hour=7),
                id='email_daily_subscriber_sync',
                name='Email CRM - Daily Subscriber Sync'
            )
            logger.info("Email CRM Agent scheduled (with daily subscriber sync)")
        
        # Sales Enablement Agent - Weekly lead review
        if 'sales_enablement' in self.agents:
            agent = self.agents['sales_enablement']
            self.scheduler.add_job(
                func=lambda: self._run_agent_task_with_context(
                    agent,
                    {'task_type': 'lead_scoring'}
                ),
                trigger=CronTrigger(day_of_week='thu', hour=10),
                id='sales_weekly_leads',
                name='Sales Enablement - Weekly Lead Scoring'
            )
            logger.info("Sales Enablement Agent scheduled")
        
        # Retention Agent - Monthly churn analysis
        if 'retention' in self.agents:
            agent = self.agents['retention']
            self.scheduler.add_job(
                func=lambda: self._run_agent_task_with_context(
                    agent,
                    {'task_type': 'churn_analysis'}
                ),
                trigger=CronTrigger(day=1, hour=14),
                id='retention_monthly_churn',
                name='Retention - Monthly Churn Analysis'
            )
            logger.info("Retention Agent scheduled")
        
        # Operations Agent - Daily system health
        if 'operations' in self.agents:
            agent = self.agents['operations']
            self.scheduler.add_job(
                func=lambda: self._run_agent_task_with_context(
                    agent,
                    {'task_type': 'system_health'}
                ),
                trigger=CronTrigger(hour=6),
                id='operations_daily_health',
                name='Operations - Daily Health Check'
            )
            logger.info("Operations Agent scheduled")
    
    def schedule_app_agent(self):
        """Schedule APP Agent - Continuous monitoring and improvement"""
        if 'app_intelligence' not in self.agents:
            return
        
        agent = self.agents['app_intelligence']
        
        # Hourly health check
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(
                agent,
                {'task_type': 'health_check'}
            ),
            trigger=IntervalTrigger(hours=1),
            id='app_hourly_health_check',
            name='APP Agent - Hourly Health Check'
        )
        
        # Daily usage analysis
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(agent, {
                'task_type': 'usage_analysis',
                'period_days': 1
            }),
            trigger=CronTrigger(hour=23, minute=30),
            id='app_daily_usage_analysis',
            name='APP Agent - Daily Usage Analysis'
        )
        
        # Weekly improvement suggestions
        self.scheduler.add_job(
            func=lambda: self._run_agent_task_with_context(agent, {
                'task_type': 'suggest_improvements',
                'context': 'weekly_review'
            }),
            trigger=CronTrigger(day_of_week='sun', hour=18),
            id='app_weekly_improvements',
            name='APP Agent - Weekly Improvement Suggestions'
        )
        
        logger.info("APP Agent scheduled")
    
    def _run_agent_task(self, agent, task_data: dict):
        """Execute an agent task and log results"""
        try:
            agent_name = agent.agent_name
            logger.info(f"Running scheduled task for {agent_name}")
            
            # Create task in database
            task_id = agent.create_task(
                task_name=task_data.get('task_type', 'scheduled_task'),
                task_data=task_data
            )
            
            # Execute task
            result = agent.execute(task_data)
            
            # Complete task
            if task_id:
                agent.complete_task(
                    task_id,
                    result,
                    status='completed' if result.get('success') else 'failed'
                )
            
            logger.info(f"Completed scheduled task for {agent_name}: {result.get('success', False)}")
            
        except Exception as e:
            logger.error(f"Error running scheduled agent task: {e}")

    def _run_agent_task_with_context(self, agent, task_data: dict):
        """Execute an agent task within the Flask app context."""
        if not self.app:
            logger.error(
                "Flask app context not set; skipping scheduled task for %s",
                agent.agent_name,
            )
            return None
        return run_agent(agent, self.app, task_data, self._run_agent_task)
    
    def start(self):
        """Start the scheduler"""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Agent Scheduler started")
    
    def stop(self):
        """Stop the scheduler"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Agent Scheduler stopped")
    
    def get_scheduled_jobs(self):
        """Get list of all scheduled jobs"""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                'id': job.id,
                'name': job.name,
                'next_run': job.next_run_time.isoformat() if job.next_run_time else None,
                'trigger': str(job.trigger)
            })
        return jobs


# Global scheduler instance
_agent_scheduler = None


def get_agent_scheduler():
    """Get or create the global agent scheduler instance"""
    global _agent_scheduler
    if _agent_scheduler is None:
        _agent_scheduler = AgentScheduler()
    return _agent_scheduler


def initialize_agent_scheduler():
    """Initialize and start the agent scheduler with all agents"""
    try:
        app = current_app._get_current_object()
        agent_classes = {}
        try:
            from agents.brand_strategy_agent import BrandStrategyAgent
            agent_classes['brand_strategy'] = BrandStrategyAgent
        except ImportError as exc:
            logger.warning("BrandStrategyAgent unavailable: %s", exc)
        try:
            from agents.content_seo_agent import ContentSEOAgent
            agent_classes['content_seo'] = ContentSEOAgent
        except ImportError as exc:
            logger.warning("ContentSEOAgent unavailable: %s", exc)
        try:
            from agents.analytics_agent import AnalyticsAgent
            agent_classes['analytics'] = AnalyticsAgent
        except ImportError as exc:
            logger.warning("AnalyticsAgent unavailable: %s", exc)
        try:
            from agents.creative_agent import CreativeAgent
            agent_classes['creative_design'] = CreativeAgent
        except ImportError as exc:
            logger.warning("CreativeAgent unavailable: %s", exc)
        try:
            from agents.advertising_agent import AdvertisingAgent
            agent_classes['advertising'] = AdvertisingAgent
        except ImportError as exc:
            logger.warning("AdvertisingAgent unavailable: %s", exc)
        try:
            from agents.social_media_agent import SocialMediaAgent
            agent_classes['social_media'] = SocialMediaAgent
        except ImportError as exc:
            logger.warning("SocialMediaAgent unavailable: %s", exc)
        try:
            from agents.email_crm_agent import EmailCRMAgent
            agent_classes['email_crm'] = EmailCRMAgent
        except ImportError as exc:
            logger.warning("EmailCRMAgent unavailable: %s", exc)
        try:
            from agents.sales_enablement_agent import SalesEnablementAgent
            agent_classes['sales_enablement'] = SalesEnablementAgent
        except ImportError as exc:
            logger.warning("SalesEnablementAgent unavailable: %s", exc)
        try:
            from agents.retention_agent import RetentionAgent
            agent_classes['retention'] = RetentionAgent
        except ImportError as exc:
            logger.warning("RetentionAgent unavailable: %s", exc)
        try:
            from agents.operations_agent import OperationsAgent
            agent_classes['operations'] = OperationsAgent
        except ImportError as exc:
            logger.warning("OperationsAgent unavailable: %s", exc)
        try:
            from agents.app_agent import AppAgent
            agent_classes['app_intelligence'] = AppAgent
        except ImportError as exc:
            logger.warning("AppAgent unavailable: %s", exc)
        
        scheduler = get_agent_scheduler()
        scheduler.set_app(app)
        
        if not agent_classes:
            logger.warning("No agents available; scheduler startup skipped.")
            return scheduler

        for agent_type, agent_class in agent_classes.items():
            scheduler.register_agent(agent_type, agent_class())
        
        # Schedule all agents
        scheduler.schedule_brand_strategy_agent()
        scheduler.schedule_content_seo_agent()
        scheduler.schedule_analytics_agent()
        scheduler.schedule_creative_agent()
        scheduler.schedule_additional_agents()
        scheduler.schedule_app_agent()
        
        # Start scheduler
        if scheduler.agents:
            scheduler.start()
        else:
            logger.warning("Scheduler has no agents; startup skipped.")
        
        logger.info("AI agents initialized and scheduled successfully")
        return scheduler
        
    except Exception as e:
        logger.error(f"Failed to initialize agent scheduler: {e}")
        return None
