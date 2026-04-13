"""Vaults and credentials tables, vault_ids on sessions

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-02 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.exec_driver_sql("""
        CREATE TABLE vaults (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            display_name TEXT NOT NULL,
            metadata     JSONB NOT NULL DEFAULT '{}',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            archived_at  TIMESTAMPTZ
        )
    """)

    conn.exec_driver_sql("""
        CREATE TABLE credentials (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            vault_id          UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
            credential_type   TEXT NOT NULL,
            mcp_server_url    TEXT,
            provider          TEXT,
            encrypted_secrets BYTEA,
            client_id         TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            archived_at       TIMESTAMPTZ
        )
    """)

    conn.exec_driver_sql("""
        CREATE UNIQUE INDEX uq_credentials_vault_mcp_url
            ON credentials (vault_id, mcp_server_url)
            WHERE mcp_server_url IS NOT NULL AND archived_at IS NULL
    """)

    conn.exec_driver_sql("""
        CREATE UNIQUE INDEX uq_credentials_vault_provider
            ON credentials (vault_id, provider)
            WHERE provider IS NOT NULL AND archived_at IS NULL
    """)

    conn.exec_driver_sql("""
        ALTER TABLE sessions ADD COLUMN vault_ids JSONB NOT NULL DEFAULT '[]'
    """)


def downgrade() -> None:
    conn = op.get_bind()

    conn.exec_driver_sql("ALTER TABLE sessions DROP COLUMN IF EXISTS vault_ids")
    conn.exec_driver_sql("DROP INDEX IF EXISTS uq_credentials_vault_provider")
    conn.exec_driver_sql("DROP INDEX IF EXISTS uq_credentials_vault_mcp_url")
    conn.exec_driver_sql("DROP TABLE IF EXISTS credentials")
    conn.exec_driver_sql("DROP TABLE IF EXISTS vaults")
