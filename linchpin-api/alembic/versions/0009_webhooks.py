"""Webhooks scaffold (v0.2.0 item #14).

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-15 00:00:00.000000

Adds the two tables that back the webhook subsystem:

- ``webhook_endpoints`` — caller-registered HTTP destinations with an
  ``enabled_events`` list (event-type allowlist) and an HMAC secret
  prefixed ``whsec_`` for signing.
- ``webhook_deliveries`` — one row per attempt to deliver a payload to
  an endpoint, with retry bookkeeping (status, next_attempt_at,
  attempts counter). The delivery worker polls
  ``status IN ('pending','failed') AND next_attempt_at <= NOW()`` and
  exponentially backs off until ``max_attempts`` is hit, at which point
  ``status='exhausted'`` retires the row.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        """
        CREATE TABLE webhook_endpoints (
            id              UUID PRIMARY KEY,
            url             TEXT NOT NULL,
            secret          TEXT NOT NULL,
            enabled_events  JSONB NOT NULL DEFAULT '[]'::jsonb,
            description     TEXT,
            enabled         BOOLEAN NOT NULL DEFAULT true,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            archived_at     TIMESTAMPTZ
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX webhook_endpoints_live_idx ON webhook_endpoints "
        "(created_at DESC) WHERE archived_at IS NULL"
    )
    conn.exec_driver_sql(
        """
        CREATE TABLE webhook_deliveries (
            id                UUID PRIMARY KEY,
            endpoint_id       UUID NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
            event_type        TEXT NOT NULL,
            payload           JSONB NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            attempts          INTEGER NOT NULL DEFAULT 0,
            last_attempt_at   TIMESTAMPTZ,
            last_status_code  INTEGER,
            last_error        TEXT,
            next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at      TIMESTAMPTZ
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX webhook_deliveries_endpoint_idx ON webhook_deliveries "
        "(endpoint_id, created_at DESC)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX webhook_deliveries_pending_idx ON webhook_deliveries "
        "(next_attempt_at) WHERE status IN ('pending','failed')"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS webhook_deliveries_pending_idx")
    conn.exec_driver_sql("DROP INDEX IF EXISTS webhook_deliveries_endpoint_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS webhook_deliveries")
    conn.exec_driver_sql("DROP INDEX IF EXISTS webhook_endpoints_live_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS webhook_endpoints")
