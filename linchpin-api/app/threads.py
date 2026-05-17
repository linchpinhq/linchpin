"""Multi-agent thread helpers (v0.6.0 PR2).

A *thread* is a sub-session: its own row, its own event log, its own
sandbox container, but linked back to a parent via
``sessions.parent_session_id``. Coordinator agents spawn threads via
``POST /v1/sessions/{id}/threads`` and watch the parent's event log
to see what the worker is doing — every event a thread appends is
mirrored into the parent's log with a ``thread_id`` payload key so
the coordinator sees one interleaved stream.

The nesting cap (default 3) keeps a coordinator-of-coordinator-of…
runaway from spiraling out of control. Three levels gives room for
parent → worker → grader without inviting trees.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from app.db import fetch_one

logger = logging.getLogger("linchpin-api.threads")


def max_thread_depth() -> int:
    """Default 3 — coordinator → worker → grader is the canonical
    pattern v0.6 is designed for. Operators can widen via env if a
    workflow legitimately needs more nesting."""
    raw = os.environ.get("LINCHPIN_MAX_THREAD_DEPTH", "3")
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


async def compute_depth(session_id: uuid.UUID) -> int:
    """Walk up the ``parent_session_id`` chain and return the depth.

    A root session has depth 0; a thread spawned by a root has depth 1;
    a thread of a thread has depth 2; …

    Cycles are not possible in correct data (the FK chain is acyclic
    by construction — each session's parent must exist before it does)
    but we cap the walk at 32 hops as a defense-in-depth so a broken
    row can't loop the API forever.
    """
    depth = 0
    current = session_id
    for _ in range(32):
        row = await fetch_one(
            "SELECT parent_session_id FROM sessions WHERE id = $1",
            current,
        )
        if row is None or row["parent_session_id"] is None:
            return depth
        current = row["parent_session_id"]
        depth += 1
    logger.error("compute_depth bailed at 32 hops for session %s", session_id)
    return depth


class ThreadDepthExceeded(Exception):
    """Raised by ``ensure_can_spawn_thread`` when nesting would breach
    the configured cap."""


async def ensure_can_spawn_thread(parent_id: uuid.UUID) -> None:
    """Reject if spawning a child off ``parent_id`` would exceed the
    configured nesting cap.

    A parent at depth d spawns a child at depth d+1. We allow up to
    ``max_thread_depth`` levels inclusive — i.e. depth 0 → depth N
    where N = max_thread_depth - 1.
    """
    depth = await compute_depth(parent_id)
    cap = max_thread_depth()
    if depth + 1 >= cap:
        raise ThreadDepthExceeded(
            f"thread nesting depth would exceed cap {cap} "
            f"(parent at depth {depth})"
        )


async def root_session_id(session_id: uuid.UUID) -> uuid.UUID:
    """Walk up to the root of the thread tree. Used by event-mirroring
    so a deep thread's events propagate all the way up to the top-level
    coordinator's stream, not just the immediate parent."""
    current = session_id
    for _ in range(32):
        row = await fetch_one(
            "SELECT parent_session_id FROM sessions WHERE id = $1",
            current,
        )
        if row is None or row["parent_session_id"] is None:
            return current
        current = row["parent_session_id"]
    return current


def thread_event_payload(thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Tag a payload with ``thread_id`` so the parent's event stream
    can attribute the event back to its origin thread. We don't
    overwrite an existing ``thread_id`` if the caller supplied one —
    that happens when a deeply-nested thread's event mirrors through
    multiple levels."""
    if "thread_id" in payload:
        return payload
    return {**payload, "thread_id": thread_id}
