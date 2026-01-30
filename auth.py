import os
import logging
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from jinja2 import TemplateNotFound
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash

from models import User

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth", template_folder="templates")

