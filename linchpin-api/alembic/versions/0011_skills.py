"""Skills (v0.4.0 — Skills).

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-16 00:00:00.000000

One table for packaged expertise bundles:

- ``skills`` — workspace-scoped, slug-named, soft-deletable. The bundle
  itself (SKILL.md + scripts/resources) lives on disk under
  ``LINCHPIN_SKILLS_ROOT`` at a content-addressed path
  ``<sha[:2]>/<sha[2:4]>/<sha>.tar.gz``. The row records the bundle's
  sha + size + the SKILL.md frontmatter (``name``, ``description``) so
  level-1 progressive disclosure can read it without unpacking.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql(
        """
        CREATE TABLE skills (
            id              UUID PRIMARY KEY,
            name            TEXT NOT NULL,
            description     TEXT NOT NULL,
            workspace_id    UUID NOT NULL,
            bundle_sha256   TEXT NOT NULL,
            bundle_size     BIGINT NOT NULL CHECK (bundle_size >= 0),
            storage_path    TEXT NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            archived_at     TIMESTAMPTZ
        )
        """
    )
    # Slug uniqueness scoped to live (non-archived) rows so a deleted skill's
    # name can be reused immediately — same pattern as memory_stores.
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX skills_name_idx "
        "ON skills (workspace_id, name) WHERE archived_at IS NULL"
    )
    conn.exec_driver_sql(
        "CREATE INDEX skills_workspace_idx ON skills (workspace_id)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP INDEX IF EXISTS skills_workspace_idx")
    conn.exec_driver_sql("DROP INDEX IF EXISTS skills_name_idx")
    conn.exec_driver_sql("DROP TABLE IF EXISTS skills")
