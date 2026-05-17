"""Agent ``skills[]`` field (v0.4.0 PR2).

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-16 00:00:01.000000

Adds a JSONB ``skills`` column to ``agents`` storing the list of skill
ids the agent has attached. Defaults to ``'[]'`` so all existing rows
behave as zero-skill agents. ``agent_versions`` mirrors the column so
the version snapshot stays faithful to the live row.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "ALTER TABLE agents ADD COLUMN skills JSONB NOT NULL DEFAULT '[]'"
    )
    conn.exec_driver_sql(
        "ALTER TABLE agent_versions ADD COLUMN skills JSONB NOT NULL DEFAULT '[]'"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("ALTER TABLE agent_versions DROP COLUMN IF EXISTS skills")
    conn.exec_driver_sql("ALTER TABLE agents DROP COLUMN IF EXISTS skills")
