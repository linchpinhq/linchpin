"""Database connection pool and helpers.

Provides asyncpg pool management, query helpers, and
Postgres LISTEN/NOTIFY utilities for event fanout.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import asyncpg

logger = logging.getLogger("linchpin-api.db")

# ---------------------------------------------------------------------------
# Module-level pool reference
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    """Create the asyncpg connection pool from DATABASE_URL.

    Stores the pool in module state so it can be retrieved via ``get_pool()``.
    """
    global _pool
    dsn = os.environ.get("DATABASE_URL", "postgresql://linchpin:linchpin@localhost:5432/linchpin")
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=20)
    logger.info("Database connection pool created.")
    return _pool


async def close_pool() -> None:
    """Close the connection pool and clear module state."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed.")


def get_pool() -> asyncpg.Pool:
    """Return the current connection pool.

    Raises ``RuntimeError`` if the pool has not been created yet.
    """
    if _pool is None:
        raise RuntimeError("Database pool is not initialised. Call create_pool() first.")
    return _pool


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


async def fetch_one(query: str, *args: Any) -> asyncpg.Record | None:
    """Execute *query* and return the first row, or ``None``."""
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetch_all(query: str, *args: Any) -> list[asyncpg.Record]:
    """Execute *query* and return all rows."""
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def execute(query: str, *args: Any) -> str:
    """Execute *query* (INSERT / UPDATE / DELETE) and return the status string."""
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)


# ---------------------------------------------------------------------------
# LISTEN / NOTIFY utilities
# ---------------------------------------------------------------------------


async def listen(channel: str) -> AsyncIterator[asyncpg.Connection]:
    """Async generator that yields notification payloads on *channel*.

    Usage::

        async for payload in listen("session_abc"):
            handle(payload)

    The generator acquires a dedicated connection from the pool and keeps it
    open for the lifetime of the iteration.  The connection is released when
    the generator is closed (e.g. via ``async for`` break or ``aclose()``).
    """
    pool = get_pool()
    conn: asyncpg.Connection = await pool.acquire()
    queue: asyncio.Queue[str] = asyncio.Queue()

    def _callback(conn: asyncpg.Connection, pid: int, channel: str, payload: str) -> None:  # noqa: ARG001
        queue.put_nowait(payload)

    try:
        await conn.add_listener(channel, _callback)
        while True:
            payload = await queue.get()
            yield payload  # type: ignore[misc]
    finally:
        await conn.remove_listener(channel, _callback)
        await pool.release(conn)


async def notify(channel: str, payload: str = "") -> None:
    """Send a NOTIFY on *channel* with an optional *payload*."""
    pool = get_pool()
    async with pool.acquire() as conn:
        # NOTIFY doesn't support parameterized queries — use literal escaping
        escaped_payload = payload.replace("'", "''")
        await conn.execute(f"NOTIFY {channel}, '{escaped_payload}'")
