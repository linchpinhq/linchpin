"""Soft-delete column for environments (v0.2.0 item #6).

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-15 00:00:00.000000

Adds ``archived_at`` to the environments table to support the archive +
delete endpoints from v0.2.0 item #6. List endpoints filter out archived
rows by default; existing sessions retain their ``environment_id`` FK
(archived envs are reachable for forensic / replay purposes).
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("ALTER TABLE environments ADD COLUMN archived_at TIMESTAMPTZ")
    # Partial index so the common-case 'live envs' list is index-only.
    conn.exec_driver_sql(
        "CREATE INDEX environments_live_idx ON environments (created_at DESC) "
        "WHERE archived_at IS NULL"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS environments_live_idx")
    conn.exec_driver_sql("ALTER TABLE environments DROP COLUMN IF EXISTS archived_at")
