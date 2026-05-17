"""Multi-agent threads — ``sessions.parent_session_id`` (v0.6.0 PR2).

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-17 00:00:01.000000

Adds a self-referential ``parent_session_id`` column to ``sessions``.
A NULL value means the row is a root (coordinator-or-standalone)
session; non-NULL means the row is a thread spawned by another
session. ``ON DELETE SET NULL`` so a removed parent doesn't cascade
through every thread.

An index on ``parent_session_id`` keeps the "list threads of a
session" query cheap.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        """
        ALTER TABLE sessions
            ADD COLUMN parent_session_id UUID
                REFERENCES sessions(id) ON DELETE SET NULL
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX sessions_parent_idx ON sessions (parent_session_id) "
        "WHERE parent_session_id IS NOT NULL"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS sessions_parent_idx")
    conn.exec_driver_sql(
        "ALTER TABLE sessions DROP COLUMN IF EXISTS parent_session_id"
    )
