"""
Error Logging System for LUX Marketing
Captures application errors for chatbot analysis and auto-repair
Works on Replit and VPS deployments
"""
import logging
import json
import os
from datetime import datetime, timedelta
from extensions import db
from sqlalchemy import func

logger = logging.getLogger(__name__)

class ErrorLog(db.Model):
    """Store application errors for chatbot analysis"""
    __tablename__ = 'error_log'
    
    id = db.Column(db.Integer, primary_key=True)
    error_type = db.Column(db.String(100))  # ValueError, TypeError, etc.
    error_message = db.Column(db.Text)
    error_stack = db.Column(db.Text)
    endpoint = db.Column(db.String(255))  # Which route failed
    method = db.Column(db.String(10))  # GET, POST, etc.
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    severity = db.Column(db.String(20), default='error')  # error, warning, critical
    is_resolved = db.Column(db.Boolean, default=False)
    resolution_notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'type': self.error_type,
            'message': self.error_message,
            'endpoint': self.endpoint,
            'method': self.method,
            'severity': self.severity,
            'resolved': self.is_resolved,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class ApplicationDiagnostics:
    """Provide system health and diagnostics for chatbot"""
    
    @staticmethod
    def get_recent_errors(hours=24, limit=10):
        """Get recent errors from the error log"""
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=hours)
            errors = ErrorLog.query.filter(
                ErrorLog.created_at >= cutoff_time
            ).order_by(ErrorLog.created_at.desc()).limit(limit).all()
            
            return [error.to_dict() for error in errors]
        except Exception as e:
            logger.error(f"Error fetching error logs: {e}")
            return []
    
    @staticmethod
    def get_error_summary():
        """Get summary of errors by type"""
        try:
            summary = db.session.query(
                ErrorLog.error_type,
                func.count(ErrorLog.id).label('count'),
                func.max(ErrorLog.created_at).label('last_occurrence')
            ).filter(
                ErrorLog.created_at >= datetime.utcnow() - timedelta(hours=24)
            ).group_by(ErrorLog.error_type).all()
            
            return [
                {
                    'type': s[0],
                    'count': s[1],
                    'last_occurrence': s[2].isoformat() if s[2] else None
                }
                for s in summary
            ]
        except Exception as e:
            logger.error(f"Error fetching error summary: {e}")
            return []
    
    @staticmethod
    def get_system_health():
        """Get overall system health status"""
        try:
            from models import Contact, Campaign, User, Company
            
            # Count recent errors
            recent_errors = ErrorLog.query.filter(
                ErrorLog.created_at >= datetime.utcnow() - timedelta(hours=1)
            ).count()
            
            unresolved_errors = ErrorLog.query.filter_by(is_resolved=False).count()
            
            # Count active data
            contacts = Contact.query.count()
            campaigns = Campaign.query.count()
            users = User.query.count()
            companies = Company.query.count()
            
            health_status = 'healthy'
            if recent_errors > 5:
                health_status = 'warning'
            if recent_errors > 20 or unresolved_errors > 10:
                health_status = 'critical'
            
            return {
                'status': health_status,
                'recent_errors_1h': recent_errors,
                'unresolved_errors': unresolved_errors,
                'active_contacts': contacts,
                'active_campaigns': campaigns,
                'users': users,
                'companies': companies,
                'timestamp': datetime.utcnow().isoformat()
            }
        except Exception as e:
            logger.error(f"Error fetching system health: {e}")
            return {'status': 'unknown', 'error': str(e)}
    
    @staticmethod
    def mark_error_resolved(error_id, resolution):
        """Mark an error as resolved"""
        try:
            error = ErrorLog.query.get(error_id)
            if error:
                error.is_resolved = True
                error.resolution_notes = resolution
                db.session.commit()
                return True
            return False
        except Exception as e:
            logger.error(f"Error marking error as resolved: {e}")
            return False

def log_application_error(error_type, error_message, endpoint='unknown', method='unknown', 
                         user_id=None, severity='error', error_stack=None):
    """Log an application error to the database"""
    try:
        error_log = ErrorLog(
            error_type=error_type,
            error_message=error_message,
            error_stack=error_stack,
            endpoint=endpoint,
            method=method,
            user_id=user_id,
            severity=severity
        )
        db.session.add(error_log)
        db.session.commit()
        logger.info(f"Logged error: {error_type} - {error_message[:100]}")
        return error_log.id
    except Exception as e:
        logger.error(f"Failed to log error: {e}")
        return None

def setup_error_logging_handler():
    """Set up Flask error handler to capture all errors"""
    from flask import request, g
    from app import app
    
    @app.errorhandler(Exception)
    def handle_error(error):
        """Capture all unhandled errors"""
        try:
            error_type = type(error).__name__
            error_message = str(error)
            endpoint = request.endpoint or 'unknown'
            method = request.method or 'unknown'
            user_id = getattr(g, 'user_id', None)
            
            # Log to database
            log_application_error(
                error_type=error_type,
                error_message=error_message,
                endpoint=endpoint,
                method=method,
                user_id=user_id,
                severity='critical' if '500' in str(error) else 'error'
            )
        except Exception as e:
            logger.error(f"Error in error handler: {e}")
        
        # Return appropriate error response
        from flask import render_template_string
        from werkzeug.exceptions import HTTPException
        
        if isinstance(error, HTTPException):
            return error
        
        # For non-HTTP exceptions, return 500
        return "Internal Server Error", 500
