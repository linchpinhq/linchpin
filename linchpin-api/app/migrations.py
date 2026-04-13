"""Alembic migration utilities for startup checks."""

import logging
import os
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool

logger = logging.getLogger("linchpin-api")

ALEMBIC_INI_PATH = Path(__file__).resolve().parent.parent / "alembic.ini"


def _get_sync_database_url() -> str:
    """Get a synchronous database URL for Alembic checks.

    Converts asyncpg-style URLs to psycopg2-compatible ones.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def check_migrations_current() -> None:
    """Verify that all Alembic migrations have been applied.

    Raises RuntimeError if the database is not at the latest revision.
    """
    alembic_cfg = Config(str(ALEMBIC_INI_PATH))
    script = ScriptDirectory.from_config(alembic_cfg)
    head_rev = script.get_current_head()

    url = _get_sync_database_url()
    engine = create_engine(url, poolclass=pool.NullPool)

    try:
        with engine.connect() as conn:
            migration_ctx = MigrationContext.configure(conn)
            current_rev = migration_ctx.get_current_revision()
    finally:
        engine.dispose()

    if current_rev != head_rev:
        raise RuntimeError(
            f"Database migrations are not up to date. "
            f"Current revision: {current_rev}, head revision: {head_rev}. "
            f"Run 'alembic upgrade head' to apply pending migrations."
        )

    logger.info("Database migrations are up to date (revision: %s).", current_rev)
