import os
from datetime import datetime

from flask import Blueprint, jsonify, render_template, current_app
from flask_login import login_required, current_user
from sqlalchemy import text

from extensions import db

main_bp = Blueprint('main', __name__)


def get_app_version() -> str:
    version_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
    try:
        with open(version_path, "r", encoding="utf-8") as version_file:
            return version_file.read().strip()
    except OSError as exc:
        current_app.logger.warning("Unable to read app version from %s: %s", version_path, exc)
        return "unknown"


def _db_status():
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        current_app.logger.error("Health check failed: %s", exc)
        return False, str(exc)
    return True, None


def _scheduler_status():
    try:
        from agent_scheduler import get_agent_scheduler
        scheduler = get_agent_scheduler()
        return "running" if scheduler.scheduler.running else "disabled"
    except Exception as exc:
        current_app.logger.warning("Scheduler status check failed: %s", exc)
        return "disabled"


@main_bp.route('/dashboard')
@login_required
def dashboard():
    return render_template(
        'v1/dashboard.html',
        user=current_user,
        app_version=get_app_version(),
    )


@main_bp.route('/health')
def health_check():
    db_ok, db_error = _db_status()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "db": "connected" if db_ok else "error",
        "auth": "ready" if "auth" in current_app.blueprints else "unavailable",
        "ai": "enabled" if os.getenv("OPENAI_API_KEY") else "disabled",
        "scheduler": _scheduler_status(),
        "version": get_app_version(),
        "timestamp": datetime.utcnow().isoformat(),
    }
    if db_error:
        payload["db_error"] = db_error[:200]
    return jsonify(payload), 200 if db_ok else 503
