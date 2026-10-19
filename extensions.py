from flask import request
from flask_limiter import Limiter
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)
csrf = CSRFProtect()


def _client_ip():
    # nginx overwrites X-Real-IP on every request and gunicorn only listens on
    # 127.0.0.1, so it is the real client; remote_addr is always the proxy.
    return request.headers.get("X-Real-IP") or request.remote_addr or "unknown"


# Per-worker in-memory counters: a brute-force brake, not a quota system.
limiter = Limiter(key_func=_client_ip, default_limits=[], storage_uri="memory://")
