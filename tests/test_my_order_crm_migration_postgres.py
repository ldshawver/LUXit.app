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
