"""Session resources table for the Resources framework (v0.2.0 PR2).

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-15 00:00:00.000000

Implements the persistence layer for item #1 of v0.2.0 (Session Resources
framework). Mirrors the schema in ``Tech Specs/v0.2.0 — Foundations``
§"New table: session_resources".

PR2 only persists the rows on session-create. Sandbox mounting lands in PR3
and live add/remove endpoints in PR4 — both extend this table.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.exec_driver_sql("""
        CREATE TABLE session_resources (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id     UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            type           TEXT NOT NULL CHECK (type IN ('file', 'memory_store', 'github_repository')),
            mount_path     TEXT NOT NULL,
            config         JSONB NOT NULL,
            state          TEXT NOT NULL DEFAULT 'mounted'
                               CHECK (state IN ('mounted', 'unmounting', 'unmounted', 'failed')),
            error          TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            unmounted_at   TIMESTAMPTZ,
            UNIQUE (session_id, mount_path)
        )
    """)

    conn.exec_driver_sql("""
        CREATE INDEX session_resources_session_idx
            ON session_resources (session_id)
            WHERE state = 'mounted'
    """)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS session_resources_session_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS session_resources")
