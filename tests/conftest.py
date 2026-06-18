"""Shared pytest configuration and fixtures.

Per-test DB isolation
---------------------
The project uses a real PostgreSQL database even during tests (Replit's
managed PostgreSQL overrides any DATABASE_URL set here via its PGHOST vars).

Each test that has an `app` fixture in scope is wrapped with the
`_db_rollback` autouse fixture, which:

  1. Opens a dedicated connection from the engine and begins an outer
     transaction (never committed).
  2. Creates a SQLAlchemy `Session` bound to that connection, configured
     with ``join_transaction_mode="create_savepoint"``.  Under this mode,
     every ``session.commit()`` call only releases and recreates a
     SAVEPOINT — it never issues a real COMMIT to PostgreSQL.
  3. Temporarily replaces ``db.session`` (the Flask-SQLAlchemy scoped
     session) with the test session, so all application code (fixtures,
     routes, helpers) transparently uses the isolated session.
  4. After the test, the outer transaction is rolled back, undoing every
     INSERT/UPDATE/DELETE that happened during the test.

This is the correct SQLAlchemy 2.x "join to external transaction" pattern.
The `begin_nested()` + `after_transaction_end` approach does NOT work in
SA 2.x because ``Session.commit()`` always commits to the root transaction.

One-time cleanup
----------------
A session-scoped autouse fixture removes test-data artifacts left by
earlier test runs (before this isolation mechanism existed).
"""

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

import sqlalchemy as sa
import pytest


# ---------------------------------------------------------------------------
# Session-scoped: clean up test artifacts from previous (pre-savepoint) runs.
# Runs once per pytest session, before any test.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _cleanup_stale_test_data():
    """Remove rows that leaked from earlier test runs with manual DELETE
    cleanup chains.  These rows have synthetic IDs that follow patterns
    introduced by the old test fixtures (cus_test_*, evt_*, cus_billing_*,
    cus_debug_*, etc.).  This fixture runs ONCE per pytest session.
    """
    try:
        from app import create_app
        from extensions import db as _db

        _app = create_app()
        with _app.app_context():
            conn = _db.engine.connect()
            with conn.begin():
                # Delete in FK-safe order using a subquery so we don't need
                # to know company IDs upfront.
                _test_cid_patterns = (
                    "cus_test_%",
                    "cus_billing_%",
                    "cus_debug_%",
                    "cus_jtm_%",
                    "cus_sp_%",
                    "cus_paid_%",
                    "cus_other_%",
                    "cus_dup_%",
                    "cus_addon_%",
                    "cus_no_%",
                )

                def _like_clause(col: str) -> str:
                    parts = " OR ".join(
                        f"{col} LIKE '{p}'" for p in _test_cid_patterns
                    )
                    return f"({parts})"

                sub = (
                    f"SELECT id FROM company WHERE {_like_clause('stripe_customer_id')}"
                    " OR (name LIKE 'Lucifer Cruz Test%' AND id > 2)"
                    " OR name LIKE '%Test Co cus_%'"
                    " OR name LIKE '_JTM_TEST_%'"
                    " OR name LIKE '_SP_DEBUG_%'"
                    " OR name LIKE '_Test_Savepoint_%'"
                )

                for tbl in (
                    "saas_automation_log",
                    "customer_onboarding_task",
                    "customer_onboarding_project",
                    "integration_event",
                    "integration_error_log",
                    "external_sync_record",
                    "company_secret",
                    "user_company_access",
                ):
                    try:
                        conn.execute(
                            sa.text(f"DELETE FROM {tbl} WHERE company_id IN ({sub})")
                        )
                    except Exception:
                        pass  # table might not have company_id

                # Delete test users created by integration fixtures
                try:
                    conn.execute(
                        sa.text(
                            "DELETE FROM \"user\" WHERE email LIKE '%@test.com'"
                            " OR username LIKE 'billtest%'"
                            " OR username LIKE 'nocust%'"
                            " OR username LIKE 'outsider%'"
                            " OR username LIKE 'paidsetup%'"
                        )
                    )
                except Exception:
                    pass

                try:
                    conn.execute(sa.text(f"DELETE FROM company WHERE id IN ({sub})"))
                except Exception:
                    pass

                # ── Schema back-fills ─────────────────────────────────────
                # Add columns that exist in the ORM model but may be missing
                # from the managed PostgreSQL schema (no migration was run).
                _backfill_ddl = [
                    "ALTER TABLE twilio_conversation ADD COLUMN IF NOT EXISTS contact_source VARCHAR(50)",
                    "ALTER TABLE twilio_conversation ADD COLUMN IF NOT EXISTS phone_number_id INTEGER",
                    "ALTER TABLE auto_reply_rule ADD COLUMN IF NOT EXISTS phone_number_id INTEGER",
                    "ALTER TABLE twilio_call_log ADD COLUMN IF NOT EXISTS phone_number_id INTEGER",
                    "ALTER TABLE notification ADD COLUMN IF NOT EXISTS phone_number_id INTEGER",
                    "ALTER TABLE notification ADD COLUMN IF NOT EXISTS event_type VARCHAR(50) DEFAULT 'system'",
                    "ALTER TABLE push_subscription ADD COLUMN IF NOT EXISTS device_key VARCHAR(120)",
                ]
                for _ddl in _backfill_ddl:
                    try:
                        conn.execute(sa.text(_ddl))
                    except Exception:
                        pass
    except Exception as exc:
        print(f"\n[conftest] stale-data cleanup skipped: {exc}")

    yield


# ---------------------------------------------------------------------------
# Function-scoped: wrap each test in a rolled-back transaction.
# ---------------------------------------------------------------------------

class _SessionProxy:
    """Wraps a plain ``SASession`` to be compatible with a Flask-SQLAlchemy
    scoped session.

    Flask-SQLAlchemy's ``_QueryProperty`` calls ``db.session()`` (with
    parentheses) on every ``Model.query`` access.  It also calls
    ``db.session.remove()`` in its ``teardown_appcontext`` handler.
    A plain ``sqlalchemy.orm.Session`` supports neither.

    This proxy:
      * Is callable (returns the wrapped session, just like a
        ``scoped_session`` would).
      * Has a no-op ``remove()`` so teardown works without errors.
      * Delegates every other attribute access / mutation to the wrapped
        session, so application code that does ``db.session.add(…)`` /
        ``db.session.commit()`` / ``db.session.query(…)`` continues to
        work unchanged.
    """

    def __init__(self, session):
        object.__setattr__(self, "_inner", session)

    # --- scoped_session compatibility ------------------------------------

    def __call__(self):
        """db.session() → return the underlying Session (not a new one)."""
        return object.__getattribute__(self, "_inner")

    def remove(self):
        """Called by Flask-SQLAlchemy teardown — no-op; cleanup is in
        _db_rollback's finally block."""

    # --- transparent delegation -----------------------------------------

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_inner"), name, value)

    def __iter__(self):
        return iter(object.__getattribute__(self, "_inner"))

    def __repr__(self):
        inner = object.__getattribute__(self, "_inner")
        return f"<_SessionProxy wrapping {inner!r}>"


@pytest.fixture(autouse=True)
def _db_rollback(request):
    """Wrap the current test in an outer PostgreSQL transaction that is
    rolled back after the test, regardless of the test outcome.

    Implementation uses SQLAlchemy 2.x "join to external transaction" pattern:

      • ``Session(connection, join_transaction_mode="create_savepoint")``
        ensures that every ``session.commit()`` in application code only
        releases/recreates a SAVEPOINT, never issues a real COMMIT.
      • ``db.session`` is temporarily replaced with the test session so
        all code paths (fixtures, Flask routes, helpers) use it
        transparently.
      • After the test, the outer ``connection.rollback()`` undoes
        everything, leaving the real database untouched.

    Tests that have no ``app`` fixture are silently skipped.
    """
    try:
        app = request.getfixturevalue("app")
    except pytest.FixtureLookupError:
        yield
        return

    from extensions import db
    from sqlalchemy.orm import Session as SASession
    from flask import has_app_context

    pushed_ctx = not has_app_context()
    if pushed_ctx:
        _ctx = app.app_context()
        _ctx.push()

    connection = db.engine.connect()
    outer_txn = connection.begin()

    # join_transaction_mode="create_savepoint":
    #   commit()  → RELEASE SAVEPOINT + new SAVEPOINT (no COMMIT to DB)
    #   rollback() → ROLLBACK TO SAVEPOINT
    test_session = SASession(connection, join_transaction_mode="create_savepoint")

    original_session = db.session
    proxy = _SessionProxy(test_session)
    db.session = proxy                 # patch: all app code now uses proxy → test_session

    # Flask 3.x stores g on the AppContext (not RequestContext).  A
    # module-scoped app fixture keeps one AppContext alive for all tests in
    # the module, so g._login_user set by an unauthenticated request persists
    # into the next test and causes Flask-Login to skip _load_user().
    # Reset it here so each test starts with a clean authentication state.
    try:
        from flask.globals import _cv_app as _flask_cv_app
        _app_ctx = _flask_cv_app.get(None)
        if _app_ctx is not None and hasattr(_app_ctx.g, "_login_user"):
            del _app_ctx.g._login_user
    except Exception:
        pass

    try:
        yield
    finally:
        db.session = original_session  # restore before teardown hooks
        try:
            test_session.close()
        except Exception:
            pass
        try:
            outer_txn.rollback()
        except Exception:
            pass
        try:
            connection.close()
        except Exception:
            pass
        if pushed_ctx:
            try:
                _ctx.pop()
            except Exception:
                pass
