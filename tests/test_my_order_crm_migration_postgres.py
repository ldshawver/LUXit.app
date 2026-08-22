"""Focused PostgreSQL-only checks for the My Order CRM migration."""
from __future__ import annotations

import os
import subprocess
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest


MIGRATION = Path(__file__).resolve().parents[1] / "migrations/20260804_my_order_crm_automation.sql"


@pytest.fixture
def pg_app():
    """Full application against real PostgreSQL, for dispatch-level (not raw-SQL) concurrency checks."""
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required for PostgreSQL migration tests")
    os.environ["TEST_DATABASE_URL"] = url
    from app import create_app
    from extensions import db as _db

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        assert _db.engine.url.get_backend_name() == "postgresql"
        _db.drop_all()
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


def _postgres_url() -> str:
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required for PostgreSQL migration tests")
    return url


def _schema() -> str:
    return "my_order_test_" + uuid.uuid4().hex


def _connect(schema: str | None = None):
    conn = psycopg2.connect(_postgres_url())
    if schema:
        with conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
    return conn


def _create_minimal_schema(schema: str) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute("CREATE TABLE company (id SERIAL PRIMARY KEY, name TEXT)")
            cur.execute("CREATE TABLE contact (id SERIAL PRIMARY KEY, company_id INTEGER REFERENCES company(id))")
            cur.execute("""
                CREATE TABLE segment (
                    id SERIAL PRIMARY KEY,
                    company_id INTEGER REFERENCES company(id),
                    name VARCHAR(255) NOT NULL,
                    segment_type VARCHAR(100),
                    match_mode VARCHAR(20) NOT NULL DEFAULT 'all',
                    triggers JSON,
                    conditions JSON,
                    actions JSON,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE
                )
            """)
            cur.execute("CREATE TABLE segment_member (id SERIAL PRIMARY KEY, segment_id INTEGER NOT NULL REFERENCES segment(id), contact_id INTEGER NOT NULL REFERENCES contact(id))")
            cur.execute("CREATE TABLE twilio_message (id SERIAL PRIMARY KEY, twilio_sid VARCHAR(100))")
        conn.commit()
    finally:
        conn.close()


def _drop_schema(schema: str) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
    finally:
        conn.close()


def _run_migration(schema: str, *, check=True):
    env = dict(os.environ)
    env["PGOPTIONS"] = f'-c search_path="{schema}"'
    return subprocess.run(
        ["psql", _postgres_url(), "-X", "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION)],
        env=env, check=check, capture_output=True, text=True,
    )


def test_postgres_partial_indexes_are_tenant_scoped_and_json_round_trips():
    schema = _schema()
    _create_minimal_schema(schema)
    try:
        _run_migration(schema)
        _run_migration(schema)  # direct SQL rerun is safe as well as ledger skipping
        conn = _connect(schema)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO company(name) VALUES ('A'),('B') RETURNING id")
                company_ids = [row[0] for row in cur.fetchall()]
                for company_id in company_ids:
                    cur.execute("INSERT INTO segment(company_id,name,segment_type) VALUES (%s,'My Order Customer','contact_tag')", (company_id,))
                    cur.execute("INSERT INTO segment(company_id,name,segment_type) VALUES (%s,'My Order Customer','behavioral')", (company_id,))
                cur.execute("INSERT INTO segment(company_id,name,segment_type,triggers,conditions,actions) VALUES (%s,'Round Trip','automation_rule',%s,%s,%s) RETURNING triggers,conditions,actions", (
                    company_ids[0], '["tag_added"]', '{"tag_ids":[1]}', '[{"type":"segment.add_contact","segment_id":2}]',
                ))
                assert cur.fetchone() == (["tag_added"], {"tag_ids": [1]}, [{"type": "segment.add_contact", "segment_id": 2}])
                cur.execute("SELECT count(*) FROM pg_indexes WHERE schemaname=%s AND indexname LIKE 'ux_segment_my_order_%%'", (schema,))
                assert cur.fetchone()[0] == 2
            conn.commit()
        finally:
            conn.close()
    finally:
        _drop_schema(schema)


@pytest.mark.parametrize("kind", ["tag", "segment", "rule"])
def test_postgres_migration_intentionally_rejects_canonical_duplicates(kind):
    schema = _schema()
    _create_minimal_schema(schema)
    try:
        conn = _connect(schema)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO company(name) VALUES ('A') RETURNING id")
                company_id = cur.fetchone()[0]
                if kind == "tag":
                    rows = [(company_id, "My Order Customer", "contact_tag"), (company_id, "MyOrder Customer", "contact_tag")]
                elif kind == "segment":
                    rows = [(company_id, "My Order Customer", "behavioral"), (company_id, "My Order Customer", "custom")]
                else:
                    rows = [(company_id, "Duplicate Rule", "automation_rule"), (company_id, " duplicate   rule ", "automation_rule")]
                cur.executemany("INSERT INTO segment(company_id,name,segment_type) VALUES (%s,%s,%s)", rows)
            conn.commit()
        finally:
            conn.close()
        result = _run_migration(schema, check=False)
        assert result.returncode != 0
        assert "could not create unique index" in result.stderr
    finally:
        _drop_schema(schema)


def test_postgres_concurrent_execution_creates_one_audit_row():
    schema = _schema()
    _create_minimal_schema(schema)
    try:
        _run_migration(schema)
        conn = _connect(schema)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO company(name) VALUES ('A') RETURNING id")
                company_id = cur.fetchone()[0]
                cur.execute("INSERT INTO contact(company_id) VALUES (%s) RETURNING id", (company_id,))
                contact_id = cur.fetchone()[0]
                cur.execute("INSERT INTO segment(company_id,name,segment_type) VALUES (%s,'Concurrency Rule','automation_rule') RETURNING id", (company_id,))
                rule_id = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def insert_once():
            worker = _connect(schema)
            try:
                barrier.wait()
                with worker.cursor() as cur:
                    cur.execute("INSERT INTO crm_automation_execution(company_id,rule_id,contact_id,event_key,action_index,trigger_name,status,details) VALUES (%s,%s,%s,'same-event',0,'tag_added','completed','{}')", (company_id, rule_id, contact_id))
                worker.commit()
                outcomes.append("inserted")
            except psycopg2.IntegrityError:
                worker.rollback()
                outcomes.append("duplicate")
            finally:
                worker.close()

        workers = [threading.Thread(target=insert_once) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        assert sorted(outcomes) == ["duplicate", "inserted"]
        conn = _connect(schema)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM crm_automation_execution WHERE event_key='same-event'")
                assert cur.fetchone()[0] == 1
        finally:
            conn.close()
    finally:
        _drop_schema(schema)


def test_postgres_concurrent_first_inbound_events_tag_contact_exactly_once(pg_app):
    """Two concurrent inbound_sms.first_for_contact events for the same contact/event_key
    must add the tag exactly once and cascade to exactly one segment membership, relying
    on real PostgreSQL unique-constraint concurrency (not app-level locking)."""
    from extensions import db
    from models import Company, Contact, CRMAutomationExecution, SegmentMember
    from services.crm_automation import FIRST_INBOUND_TRIGGER, dispatch_event, ensure_my_order_automation

    with pg_app.app_context():
        company = Company(name="Concurrent First Inbound")
        db.session.add(company)
        db.session.flush()
        contact = Contact(company_id=company.id, is_active=True)
        db.session.add(contact)
        db.session.flush()
        records = ensure_my_order_automation(company.id)
        db.session.commit()
        company_id, contact_id = company.id, contact.id
        rule1_id, segment_id = records["rules"][0].id, records["segment"].id

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def fire_once():
        with pg_app.app_context():
            barrier.wait()
            try:
                dispatch_event(
                    company_id, contact_id, FIRST_INBOUND_TRIGGER, "same-first-event",
                    direction="inbound", channel="sms", first_inbound_sms=True,
                    message_sid="SM-CONCURRENT-FIRST",
                )
                db.session.commit()
                outcomes.append("ran")
            except Exception:
                db.session.rollback()
                outcomes.append("errored")
            finally:
                db.session.remove()

    workers = [threading.Thread(target=fire_once) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert outcomes == ["ran", "ran"]
    with pg_app.app_context():
        assert CRMAutomationExecution.query.filter_by(
            company_id=company_id, rule_id=rule1_id, event_key="same-first-event", action_index=0,
        ).count() == 1
        contact = db.session.get(Contact, contact_id)
        assert sum(1 for t in (contact.tags or "").split(",") if t.strip() == "My Order Customer") == 1
        assert SegmentMember.query.filter_by(segment_id=segment_id, contact_id=contact_id).count() == 1


def test_postgres_concurrent_tag_added_events_create_one_segment_membership(pg_app):
    """Two concurrent tag_added events for the same contact/tag must create exactly one
    segment membership under real PostgreSQL concurrency."""
    from extensions import db
    from models import Company, Contact, CRMAutomationExecution, SegmentMember
    from services.crm_automation import TAG_ADDED_TRIGGER, dispatch_event, ensure_my_order_automation

    with pg_app.app_context():
        company = Company(name="Concurrent Tag Added")
        db.session.add(company)
        db.session.flush()
        contact = Contact(company_id=company.id, is_active=True, tags="My Order Customer")
        db.session.add(contact)
        db.session.flush()
        records = ensure_my_order_automation(company.id)
        db.session.commit()
        company_id, contact_id = company.id, contact.id
        tag_id, rule2_id, segment_id = records["tag"].id, records["rules"][1].id, records["segment"].id

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def fire_once():
        with pg_app.app_context():
            barrier.wait()
            try:
                dispatch_event(
                    company_id, contact_id, TAG_ADDED_TRIGGER, "same-tag-added-event",
                    tag_id=tag_id, tag_name="My Order Customer",
                )
                db.session.commit()
                outcomes.append("ran")
            except Exception:
                db.session.rollback()
                outcomes.append("errored")
            finally:
                db.session.remove()

    workers = [threading.Thread(target=fire_once) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert outcomes == ["ran", "ran"]
    with pg_app.app_context():
        assert CRMAutomationExecution.query.filter_by(
            company_id=company_id, rule_id=rule2_id, event_key="same-tag-added-event", action_index=0,
        ).count() == 1
        assert SegmentMember.query.filter_by(segment_id=segment_id, contact_id=contact_id).count() == 1
