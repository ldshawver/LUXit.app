import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from extensions import db
from models import Campaign
from email_service import EmailService

scheduler = None


def run_scheduled_campaign(campaign_id, app):
    """Run a scheduled campaign inside the Flask app context."""
    with app.app_context():
        logging.info(
            "Running scheduled campaign %s within Flask app context",
            campaign_id,
        )
        return send_scheduled_campaign(campaign_id)

def send_scheduled_campaign(campaign_id):
    """Send a scheduled campaign"""
    try:
        campaign = Campaign.query.get(campaign_id)
        if not campaign:
            logging.error(f"Campaign {campaign_id} not found")
            return
        
        if campaign.status != 'scheduled':
            logging.warning(f"Campaign {campaign_id} is not in scheduled status")
            return
        
        logging.info(f"Sending scheduled campaign: {campaign.name}")
        
        email_service = EmailService()
        result = email_service.send_campaign(campaign)
        
        logging.info(f"Scheduled campaign {campaign_id} completed: {result}")
        
    except Exception as e:
        logging.error(f"Error sending scheduled campaign {campaign_id}: {str(e)}")
        
        # Update campaign status to failed
        campaign = Campaign.query.get(campaign_id)
        if campaign:
            campaign.status = 'failed'
            db.session.commit()

def schedule_campaign(campaign, app=None):
    """Schedule a campaign to be sent at specified time"""
    if not campaign.scheduled_at:
        return

    if app is None:
        logging.error(
            "Cannot schedule campaign %s without Flask app context",
            campaign.id,
        )
        return
    
    job_id = f"campaign_{campaign.id}"
    
    try:
        # Remove existing job if it exists
        try:
            scheduler.remove_job(job_id)
        except:
            pass
        
        # Schedule new job
        scheduler.add_job(
            func=run_scheduled_campaign,
            trigger="date",
            run_date=campaign.scheduled_at,
            args=[campaign.id, app],
            id=job_id,
            name=f"Send campaign: {campaign.name}",
            misfire_grace_time=300  # 5 minutes
        )
        
        logging.info(f"Scheduled campaign {campaign.id} for {campaign.scheduled_at}")
        
    except Exception as e:
        logging.error(f"Error scheduling campaign {campaign.id}: {str(e)}")

def init_scheduler(app):
    """Initialize the background scheduler"""
    global scheduler
    
    if scheduler is not None:
        return scheduler
    
    # Configure job store
    jobstores = {
        'default': SQLAlchemyJobStore(url=app.config['SQLALCHEMY_DATABASE_URI'])
    }
    
    executors = {
        'default': ThreadPoolExecutor(20)
    }
    
    job_defaults = {
        'coalesce': False,
        'max_instances': 3
    }
    
    scheduler = BackgroundScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
        timezone='UTC'
    )
    
    # Start scheduler
    scheduler.start()
    
    # Schedule any existing campaigns that are due
    with app.app_context():
        scheduled_campaigns = Campaign.query.filter_by(status='scheduled').all()
        for campaign in scheduled_campaigns:
            if campaign.scheduled_at and campaign.scheduled_at > datetime.utcnow():
                schedule_campaign(campaign, app)
            else:
                # Past due, mark as failed
                campaign.status = 'failed'
                db.session.commit()
    
    logging.info("Email scheduler initialized")
    return scheduler

def shutdown_scheduler():
    """Shutdown the scheduler"""
    global scheduler
    if scheduler:
        scheduler.shutdown()
        scheduler = None
