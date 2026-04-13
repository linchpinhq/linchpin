"""Initial schema: agents, environments, sessions, events

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))

    op.execute(sa.text("""
        CREATE TABLE agents (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT NOT NULL,
            version     INTEGER NOT NULL DEFAULT 1,
            model       JSONB NOT NULL,
            system      TEXT NOT NULL,
            tools       JSONB NOT NULL DEFAULT '[]',
            mcp_servers JSONB NOT NULL DEFAULT '[]',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """))

    op.execute(sa.text("""
        CREATE TABLE environments (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name        TEXT NOT NULL,
            config      JSONB NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """))

    # Use connection.exec_driver_sql to avoid SQLAlchemy interpreting :0 as bind params
    conn = op.get_bind()
    conn.exec_driver_sql("""
        CREATE TABLE sessions (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_id          UUID NOT NULL REFERENCES agents(id),
            agent_version     INTEGER NOT NULL,
            environment_id    UUID NOT NULL REFERENCES environments(id),
            status            TEXT NOT NULL DEFAULT 'running',
            container_id      TEXT,
            title             TEXT,
            metadata          JSONB NOT NULL DEFAULT '{}',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            archived_at       TIMESTAMPTZ,
            last_event_cursor TEXT,
            ttl_seconds       INTEGER,
            stats             JSONB NOT NULL DEFAULT '{"total_events":0,"tool_calls":0,"model_turns":0}',
            usage             JSONB NOT NULL DEFAULT '{"input_tokens":0,"output_tokens":0}'
        )
    """)

    op.execute(sa.text("""
        CREATE TABLE events (
            session_id   UUID NOT NULL REFERENCES sessions(id),
            cursor       TEXT NOT NULL,
            seq          INTEGER NOT NULL,
            type         TEXT NOT NULL,
            payload      JSONB NOT NULL DEFAULT '{}',
            processed_at TIMESTAMPTZ,
            PRIMARY KEY (session_id, seq)
        )
    """))

    op.execute(sa.text(
        "CREATE INDEX idx_events_cursor ON events (session_id, cursor)"
    ))


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_events_cursor")
    op.execute("DROP TABLE IF EXISTS events")
    op.execute("DROP TABLE IF EXISTS sessions")
    op.execute("DROP TABLE IF EXISTS environments")
    op.execute("DROP TABLE IF EXISTS agents")
