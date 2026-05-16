"""Memory Stores (v0.3.0 — Memory).

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-15 00:00:00.000000

Three tables for persistent, workspace-scoped agent memory:

- ``memory_stores`` — top-level container. Slug-named, unique per
  workspace, soft-deletable.
- ``memories`` — path-addressed entries inside a store
  (``/preferences/formatting.md``). 100 KB cap. Soft-deleted so the
  version chain remains intact.
- ``memory_versions`` — immutable snapshot per write, with retention
  metadata (``expires_at``) and a tombstone slot (``redacted_at``) for
  the GC + explicit redact paths.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        """
        CREATE TABLE memory_stores (
            id            UUID PRIMARY KEY,
            name          TEXT NOT NULL,
            description   TEXT,
            workspace_id  UUID NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            archived_at   TIMESTAMPTZ
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX memory_stores_name_idx "
        "ON memory_stores (workspace_id, name) WHERE archived_at IS NULL"
    )

    conn.exec_driver_sql(
        """
        CREATE TABLE memories (
            id              UUID PRIMARY KEY,
            memory_store_id UUID NOT NULL REFERENCES memory_stores(id) ON DELETE CASCADE,
            path            TEXT NOT NULL,
            content_sha256  TEXT NOT NULL,
            size_bytes      INTEGER NOT NULL CHECK (size_bytes >= 0 AND size_bytes <= 102400),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at      TIMESTAMPTZ
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX memories_path_idx "
        "ON memories (memory_store_id, path) WHERE deleted_at IS NULL"
    )
    conn.exec_driver_sql(
        "CREATE INDEX memories_store_idx "
        "ON memories (memory_store_id) WHERE deleted_at IS NULL"
    )

    conn.exec_driver_sql(
        """
        CREATE TABLE memory_versions (
            id              UUID PRIMARY KEY,
            memory_id       UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            memory_store_id UUID NOT NULL REFERENCES memory_stores(id) ON DELETE CASCADE,
            seq             INTEGER NOT NULL,
            content_sha256  TEXT NOT NULL,
            size_bytes      INTEGER NOT NULL,
            storage_path    TEXT NOT NULL,
            author          TEXT NOT NULL,
            action          TEXT NOT NULL,
            redacted_at     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at      TIMESTAMPTZ NOT NULL,
            CONSTRAINT memory_versions_seq_unique UNIQUE (memory_id, seq)
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX memory_versions_store_idx "
        "ON memory_versions (memory_store_id, created_at DESC)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX memory_versions_expiry_idx "
        "ON memory_versions (expires_at) WHERE redacted_at IS NULL"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS memory_versions_expiry_idx")
    conn.exec_driver_sql("DROP INDEX IF EXISTS memory_versions_store_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS memory_versions")
    conn.exec_driver_sql("DROP INDEX IF EXISTS memories_store_idx")
    conn.exec_driver_sql("DROP INDEX IF EXISTS memories_path_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS memories")
    conn.exec_driver_sql("DROP INDEX IF EXISTS memory_stores_name_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS memory_stores")
