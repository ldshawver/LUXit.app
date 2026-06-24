from __future__ import annotations
from sqlalchemy import text


def insert_appdoctor_run_sql():
    return text("""
        INSERT INTO appdoctor_runs
          (company_id, user_id, model, prompt, response, status, error_message, metadata, started_at, completed_at)
        VALUES
          (CAST(:company_id AS integer), CAST(:user_id AS integer), CAST(:model AS text), CAST(:prompt AS text),
           CAST(:response AS text), CAST(:status AS text), CAST(:error_message AS text), CAST(:metadata AS jsonb),
           CAST(:started_at AS timestamptz), CAST(:completed_at AS timestamptz))
        RETURNING id
    """)
