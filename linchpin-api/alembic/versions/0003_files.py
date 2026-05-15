"""Files table for the Files API (v0.2.0 PR1).

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-13 00:00:00.000000

Implements the storage layer for items #1 / #2 of v0.2.0 — the Files API
source of truth for binary content. Mirrors the schema in
``docs/Tech Specs/v0.2.0 — Foundations`` §"New table: files".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.exec_driver_sql("""
        CREATE TABLE files (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            filename      TEXT NOT NULL,
            content_type  TEXT NOT NULL,
            size_bytes    BIGINT NOT NULL CHECK (size_bytes >= 0),
            storage_path  TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            source        TEXT NOT NULL CHECK (source IN ('upload', 'deliverable')),
            downloadable  BOOLEAN NOT NULL,
            scope_type    TEXT CHECK (scope_type IS NULL OR scope_type IN ('session')),
            scope_id      UUID,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            archived_at   TIMESTAMPTZ,
            CONSTRAINT files_scope_consistency CHECK (
                (scope_type IS NULL AND scope_id IS NULL)
                OR (scope_type IS NOT NULL AND scope_id IS NOT NULL)
            )
        )
    """)

    conn.exec_driver_sql("""
        CREATE INDEX files_scope_idx
            ON files (scope_type, scope_id)
            WHERE archived_at IS NULL
    """)

    conn.exec_driver_sql("CREATE INDEX files_sha_idx ON files (sha256)")


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS files_sha_idx")
    conn.exec_driver_sql("DROP INDEX IF EXISTS files_scope_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS files")
