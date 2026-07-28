"""Corrective migration proof in an isolated schema of the disposable PG DB."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import pytest


SCHEMA = "identity_hardening_migration_test"


def test_corrective_migration_repairs_poisoned_and_malformed_state_idempotently():
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL-only migration test")
    conn = psycopg2.connect(url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), inet_server_port()")
            assert cur.fetchone() == ("lux_identity_hardening_test", 5433)
            cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
            cur.execute(f'CREATE SCHEMA "{SCHEMA}"')
            cur.execute(f'SET search_path TO "{SCHEMA}"')
            cur.execute("""
                CREATE TABLE company (id SERIAL PRIMARY KEY);
                CREATE TABLE "user" (id SERIAL PRIMARY KEY);
                CREATE TABLE auto_reply_rule (id SERIAL PRIMARY KEY);
                CREATE TABLE sms_campaign (id SERIAL PRIMARY KEY);
                CREATE TABLE twilio_conversation (
                  id SERIAL PRIMARY KEY,
                  company_id INTEGER NOT NULL REFERENCES company(id)
                );
                CREATE TABLE twilio_message (
                  id SERIAL PRIMARY KEY,
                  company_id INTEGER NOT NULL REFERENCES company(id),
                  conversation_id INTEGER NOT NULL REFERENCES twilio_conversation(id),
                  twilio_sid VARCHAR(100) UNIQUE,
                  direction VARCHAR(10) NOT NULL,
                  body TEXT
                );
                CREATE TABLE contact (
                  id SERIAL PRIMARY KEY,
                  company_id INTEGER REFERENCES company(id),
                  phone VARCHAR(50), primary_phone VARCHAR(50), normalized_phone VARCHAR(32),
                  email VARCHAR(255), primary_email VARCHAR(255), normalized_email VARCHAR(255),
                  first_name VARCHAR(120), last_name VARCHAR(120), name VARCHAR(255),
                  display_name VARCHAR(255), is_active BOOLEAN DEFAULT TRUE,
                  merged_into_contact_id INTEGER,
                  identity_status VARCHAR(32) NOT NULL DEFAULT 'pending_identity',
                  identity_verified_at TIMESTAMP,
                  name_verification_level VARCHAR(32) NOT NULL DEFAULT 'unverified',
                  name_source VARCHAR(80),
                  name_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
                  pending_first_name VARCHAR(120), pending_last_name VARCHAR(120),
                  pending_email VARCHAR(255),
                  identity_requested_fields JSONB NOT NULL DEFAULT '[]'::jsonb,
                  identity_request_state JSONB NOT NULL DEFAULT '{}'::jsonb,
                  identity_fields_requested_at TIMESTAMP,
                  identity_last_request_sid VARCHAR(64),
                  email_consent_status VARCHAR(30),
                  sms_consent_status VARCHAR(30),
                  do_not_email BOOLEAN NOT NULL DEFAULT FALSE,
                  email_unsubscribed BOOLEAN NOT NULL DEFAULT FALSE,
                  do_not_sms BOOLEAN NOT NULL DEFAULT FALSE,
                  sms_opted_out BOOLEAN NOT NULL DEFAULT FALSE
                );
                CREATE TABLE contact_phone_number (
                  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id),
                  contact_id INTEGER NOT NULL REFERENCES contact(id),
                  original_value VARCHAR(80), normalized_value VARCHAR(32)
                );
                CREATE TABLE contact_email_address (
                  id SERIAL PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES company(id),
                  contact_id INTEGER NOT NULL REFERENCES contact(id),
                  original_value VARCHAR(255), normalized_value VARCHAR(255)
                );
            """)
            cur.execute("""
                INSERT INTO company DEFAULT VALUES;
                INSERT INTO twilio_conversation(company_id) VALUES (1);
                INSERT INTO twilio_message(company_id, conversation_id, twilio_sid, direction, body)
                  VALUES (1, 1, 'SM-HISTORY', 'inbound', 'preserve me');
                INSERT INTO contact(
                  company_id, first_name, last_name, name, display_name,
                  identity_status, identity_verified_at, name_verification_level,
                  name_source, name_provenance, email_consent_status, sms_consent_status
                ) VALUES
                  (1, 'Grace', 'Hopper', 'Grace Hopper', 'Grace Hopper', 'confirmed', NOW(),
                   'verified', 'manual', '{"source":"manual"}', 'opted_in', 'opted_in'),
                  (1, 'Need', 'More Information', 'Need More Information', 'Need More Information',
                   'confirmed', NOW(), 'verified', 'customer_confirmed',
                   '{"source":"customer_confirmed_sms"}', 'unsubscribed', 'opted_out');
                INSERT INTO contact(
                  company_id, identity_status, pending_first_name, pending_last_name,
                  pending_email, identity_fields_requested_at, identity_last_request_sid,
                  identity_request_state, do_not_email, email_unsubscribed
                ) VALUES
                  (1, 'awaiting_confirmation', 'Luke', 'Shawver', 'luke@adiken.com',
                   NOW(), 'SM-REQUEST',
                   '{"phase":"awaiting_confirmation","company_id":1,"contact_id":3,
                     "confirmation_nonce":"SM-REQUEST","requested_at":"garbage"}',
                   TRUE, TRUE);
                INSERT INTO contact_email_address(
                  company_id, contact_id, original_value, normalized_value
                ) VALUES (1, 1, ' Luke@Adiken.com ', ' LUKE@ADIKEN.COM ');
            """)
            before = {}
            cur.execute("SELECT count(*) FROM contact")
            before["contacts"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM twilio_message")
            before["messages"] = cur.fetchone()[0]

            migration = Path("migrations/20260727_inbound_identity_resolution_hardening.sql").read_text()
            cur.execute(migration)
            cur.execute(migration)

            cur.execute("""
                SELECT identity_status, name_verification_level, name_source,
                       display_name, name_provenance->>'legacy_untrusted_name'
                FROM contact WHERE id=2
            """)
            assert cur.fetchone() == (
                "awaiting_name", "unverified", "legacy_untrusted", None,
                "Need More Information",
            )
            cur.execute("""
                SELECT identity_status, name_verification_level, name_source, display_name
                FROM contact WHERE id=1
            """)
            assert cur.fetchone() == ("confirmed", "verified", "manual", "Grace Hopper")
            cur.execute("""
                SELECT identity_status, pending_first_name, pending_email,
                       identity_request_state, do_not_email, email_unsubscribed
                FROM contact WHERE id=3
            """)
            state = cur.fetchone()
            assert state[:4] == ("awaiting_name", None, None, {})
            assert state[4:] == (True, True)
            cur.execute("SELECT normalized_value FROM contact_email_address WHERE id=1")
            assert cur.fetchone()[0] == "luke@adiken.com"
            cur.execute("SELECT count(*) FROM contact")
            assert cur.fetchone()[0] == before["contacts"]
            cur.execute("SELECT count(*) FROM twilio_message WHERE body='preserve me'")
            assert cur.fetchone()[0] == before["messages"]
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        finally:
            conn.close()
