import os
import sys


def pytest_configure(config):
    """Set testing env vars before any test module is imported.

    FLASK_ENV=testing activates the shortcut in _resolve_db_url() so the
    app always uses an ephemeral SQLite in-memory database during pytest
    runs — the live Postgres database is never touched.
    """
    os.environ["FLASK_ENV"] = "testing"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ.setdefault("OPENAI_API_KEY", "test")
    os.environ.setdefault(
        "DATA_ENCRYPTION_KEY",
        "g2CDXwdc6VKAElQ5QWqFBCsmXL_dQAs3e44_Gl1oJaU=",
    )


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
