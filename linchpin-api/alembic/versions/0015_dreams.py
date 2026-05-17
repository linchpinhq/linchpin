"""Dreams — async memory curation (v0.7.0 PR1).

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-17 00:00:02.000000

One table for the curation pipeline. A Dream reads an input memory
store + a filtered slice of past sessions and produces a new memory
store. The actual curation runs as a normal Linchpin session (the
"dreamer" agent), so this table just tracks the lifecycle + the
linkage between input store, dreamer session, and output store.

``ON DELETE SET NULL`` is used everywhere a Dream references another
row so deleting an input store or the dreamer session doesn't cascade
through the Dream history.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        """
        CREATE TABLE dreams (
            id                       UUID PRIMARY KEY,
            workspace_id             UUID NOT NULL,
            input_memory_store_id    UUID NOT NULL
                REFERENCES memory_stores(id) ON DELETE SET NULL,
            output_memory_store_id   UUID
                REFERENCES memory_stores(id) ON DELETE SET NULL,
            output_store_name        TEXT NOT NULL,
            dreamer_session_id       UUID
                REFERENCES sessions(id) ON DELETE SET NULL,
            dreamer_agent_id         UUID
                REFERENCES agents(id) ON DELETE SET NULL,
            session_filter           JSONB NOT NULL DEFAULT '{}',
            status                   TEXT NOT NULL
                CHECK (status IN ('pending', 'running',
                                  'completed', 'failed', 'canceled')),
            error                    TEXT,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at               TIMESTAMPTZ,
            ended_at                 TIMESTAMPTZ
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX dreams_workspace_idx "
        "ON dreams (workspace_id, created_at DESC)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX dreams_status_idx ON dreams (status) "
        "WHERE status IN ('pending', 'running')"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS dreams_status_idx")
    conn.exec_driver_sql("DROP INDEX IF EXISTS dreams_workspace_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS dreams")
