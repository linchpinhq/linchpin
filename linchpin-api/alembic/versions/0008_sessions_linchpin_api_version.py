"""Per-session API-version pin (v0.2.0 breaking bundle).

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-15 00:00:00.000000

Stores the ``Linchpin-API-Version`` header value that was active when the
session was created. NULL = no header sent = v0.1 wire shapes (legacy).
``'2026-05-13'`` = first v0.2 wire-shape generation.

Subsequent reads of session-scoped resources (events, SSE, session-get)
serialize their output in the shape the session was *created* with, so a
long-running v0.1-shape client still consuming an in-flight session keeps
seeing the events shape it expects through to terminate.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "ALTER TABLE sessions ADD COLUMN linchpin_api_version TEXT"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        "ALTER TABLE sessions DROP COLUMN IF EXISTS linchpin_api_version"
    )
