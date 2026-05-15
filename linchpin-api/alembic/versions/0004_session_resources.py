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

    # IF NOT EXISTS makes the upgrade idempotent — symmetric with downgrade's
    # DROP IF EXISTS, and survives partial-failure replays.
    #
    # State-consistency CHECKs:
    #   - state='failed' MUST have an error message (so /v1/sessions/{id}/resources
    #     can surface the failure reason to the caller).
    #   - state IN ('unmounted','failed') MUST have unmounted_at set (audit trail).
    # These invariants will be set by PR3/PR4's state transition logic; we enforce
    # them at the DB so a buggy state transition can't write inconsistent rows.
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS session_resources (
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
            UNIQUE (session_id, mount_path),
            CONSTRAINT session_resources_failed_has_error
                CHECK (state <> 'failed' OR error IS NOT NULL),
            CONSTRAINT session_resources_terminal_has_unmounted_at
                CHECK (state NOT IN ('unmounted', 'failed') OR unmounted_at IS NOT NULL)
        )
    """)

    conn.exec_driver_sql("""
        CREATE INDEX IF NOT EXISTS session_resources_session_idx
            ON session_resources (session_id)
            WHERE state = 'mounted'
    """)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS session_resources_session_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS session_resources")
