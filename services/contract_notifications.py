from __future__ import annotations
from sqlalchemy import text


def activation_recipients_sql() -> str:
    return """
        SELECT u.id AS user_id,
               u.email,
               COALESCE(NULLIF(c.phone, ''), NULLIF(cs.phone, '')) AS phone
          FROM contract_signers cs
          LEFT JOIN "user" u ON u.id = cs.user_id
          LEFT JOIN contact c ON c.id = cs.contact_id
         WHERE cs.contract_id = CAST(:contract_id AS uuid)
    """


def fetch_activation_recipients(db_session, contract_id: str):
    return [dict(r._mapping) for r in db_session.execute(text(activation_recipients_sql()), {"contract_id": contract_id})]
