"""Expose the single Flask application factory defined in app.py."""
from app import app as app  # noqa: F401
from app import create_app as _root_create_app


def create_app(*, testing: bool = False):
    """Compatibility wrapper for tests importing lux.create_app(testing=True)."""
    flask_app = _root_create_app()
    if testing:
        flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SERVER_NAME="localhost")
    return flask_app
