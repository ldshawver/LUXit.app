#!/usr/bin/env python3
"""
Health check endpoint for monitoring
Add this to your routes.py or create a separate health check endpoint
"""

from flask import jsonify, current_app
from datetime import datetime
try:
    import psutil
except ImportError:
    psutil = None
import os

def health_check():
    """
    Comprehensive health check endpoint
    Returns JSON with system status
    """
    try:
        # Check database connection
        from extensions import db
        db.session.execute('SELECT 1')
        db_status = "healthy"
    except Exception as e:
        db_status = f"error: {str(e)}"
    
    # System metrics
    system_info = {
        "timestamp": datetime.utcnow().isoformat(),
        "status": "healthy" if db_status == "healthy" else "unhealthy",
        "database": db_status,
    }
    
    # Add system metrics if psutil is available
    if psutil:
        system_info.update({
            "memory_usage": {
                "used": psutil.virtual_memory().used,
                "total": psutil.virtual_memory().total,
                "percent": psutil.virtual_memory().percent
            },
            "disk_usage": {
                "used": psutil.disk_usage('/').used,
                "total": psutil.disk_usage('/').total,
                "percent": psutil.disk_usage('/').percent
            },
            "cpu_percent": psutil.cpu_percent(interval=1),
            "load_average": os.getloadavg(),
        })
    
    system_info["uptime"] = datetime.utcnow().isoformat()  # App start time would be better
    
    status_code = 200 if system_info["status"] == "healthy" else 503
    return jsonify(system_info), status_code

# Add this route to your routes.py:
# @main_bp.route('/health')
# def health():
#     return health_check()