"""Partial unique index on files(scope_id, sha256) for session deliverables (v0.2.0 PR5 follow-up).

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-15 00:00:00.000000

The deliverables watcher (PR5) inserts files with
``ON CONFLICT DO NOTHING`` intending to dedupe identical content within a
session. The original ``files`` table (migration 0003) only has a unique
key on the primary key ``id`` — which is freshly generated per call — so
the conflict clause never fires and the dedup is carried entirely by an
application-level ``SELECT 1 WHERE sha256=$2`` check that has a TOCTOU
window before the INSERT.

This migration installs the missing partial unique index so the dedup is
real at the DB level. Scoped to session-source rows so it doesn't
constrain uploads or future scope types. Partial on ``archived_at IS NULL``
so soft-deleted rows don't block re-ingest after an archive.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        CREATE UNIQUE INDEX files_session_sha_uidx
            ON files (scope_id, sha256)
            WHERE scope_type = 'session' AND archived_at IS NULL
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP INDEX IF EXISTS files_session_sha_uidx")
