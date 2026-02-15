from flask import Blueprint, render_template
from datetime import datetime

marketing_bp = Blueprint('marketing', __name__, url_prefix='/m')


@marketing_bp.context_processor
def inject_now():
    return {'now': datetime.utcnow()}


@marketing_bp.route('/')
def home():
    return render_template('marketing/home.html', active_page='home')


@marketing_bp.route('/features')
def features():
    return render_template('marketing/features.html', active_page='features')


@marketing_bp.route('/solutions')
def solutions():
    return render_template('marketing/solutions.html', active_page='solutions')


@marketing_bp.route('/security')
def security():
    return render_template('marketing/security.html', active_page='security')
