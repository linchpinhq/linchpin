"""Agent versioning + archive + description + metadata (v0.2.0 item #5).

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-15 00:00:00.000000

Adds three things to support the v0.2.0 item #5 surface:

1. ``agents.archived_at`` — soft-delete column matching the env pattern from
   migration 0005. List endpoints filter archived rows by default.
2. ``agents.description`` (TEXT) + ``agents.metadata`` (JSONB) — Anthropic
   Managed Agents parity fields. Both nullable / empty-default so existing
   v0.1 rows don't need backfill.
3. New ``agent_versions`` table — snapshot of agent state taken on every
   PATCH **before** the update lands. Lets ``GET /v1/agents/{id}/versions``
   return historical config, which sessions use to pin agent_version on
   the session row.

Chains off ``0005`` (env archive from item #6) — both originally chained
off ``0004``; this one rebased to land second.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Soft-delete + new descriptive fields on the agent row.
    conn.exec_driver_sql("ALTER TABLE agents ADD COLUMN archived_at TIMESTAMPTZ")
    conn.exec_driver_sql("ALTER TABLE agents ADD COLUMN description TEXT")
    conn.exec_driver_sql("ALTER TABLE agents ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}'")

    # Partial index for the live-agents list (common case).
    conn.exec_driver_sql(
        "CREATE INDEX agents_live_idx ON agents (created_at DESC) "
        "WHERE archived_at IS NULL"
    )

    # agent_versions: append-only history of every PATCH'd agent state.
    # version is unique per agent so a UI / SDK can fetch a specific snapshot
    # via (agent_id, version).
    conn.exec_driver_sql("""
        CREATE TABLE agent_versions (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_id    UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            version     INTEGER NOT NULL,
            name        TEXT NOT NULL,
            description TEXT,
            metadata    JSONB NOT NULL DEFAULT '{}',
            model       JSONB NOT NULL,
            system      TEXT NOT NULL,
            tools       JSONB NOT NULL DEFAULT '[]',
            mcp_servers JSONB NOT NULL DEFAULT '[]',
            snapshotted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (agent_id, version)
        )
    """)
    conn.exec_driver_sql(
        "CREATE INDEX agent_versions_agent_idx "
        "ON agent_versions (agent_id, version DESC)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS agent_versions_agent_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS agent_versions")
    conn.exec_driver_sql("DROP INDEX IF EXISTS agents_live_idx")
    conn.exec_driver_sql("ALTER TABLE agents DROP COLUMN IF EXISTS metadata")
    conn.exec_driver_sql("ALTER TABLE agents DROP COLUMN IF EXISTS description")
    conn.exec_driver_sql("ALTER TABLE agents DROP COLUMN IF EXISTS archived_at")
