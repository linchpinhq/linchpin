"""Dreams API endpoints (v0.7.0 — research preview).

POST   /v1/dreams              — start a Dream
GET    /v1/dreams              — list dreams (newest first)
GET    /v1/dreams/{id}         — status + output store id when complete
POST   /v1/dreams/{id}/cancel  — cancel a running dream

A Dream is itself a managed agent session. This route lifecycle-
tracks the curation pass (status, input + output store, dreamer
session id) so callers can poll without scraping the session event
stream — and so a future scheduler can fan out dreams in parallel.

The actual curation logic lives in the dreamer agent's system
prompt + tools. The agent id comes from ``LINCHPIN_DREAMER_AGENT_ID``
(an operator-configured agent the platform expects to exist); a
caller can override per-dream via ``dreamer_agent_id`` on the
request body.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, HTTPException, Query

from app.db import execute, fetch_all, fetch_one
from app.models import (
    CreateDreamRequest,
    DREAM_TERMINAL_STATES,
    Dream,
    DreamListResponse,
    DreamSessionFilter,
    DreamStatus,
)

logger = logging.getLogger("linchpin-api")

router = APIRouter(prefix="/dreams", tags=["dreams"])


# Mirrors the memory_stores / skills approach — single-tenant
# placeholder until multi-tenancy lands.
_DEFAULT_WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000000")


def default_dreamer_agent_id() -> str | None:
    """The platform-default dreamer agent. NULL means dreams will be
    rejected unless the caller passes ``dreamer_agent_id`` per-request,
    which is exactly the v0.7 research-preview ergonomics — operators
    explicitly opt in by setting the env var."""
    raw = os.environ.get("LINCHPIN_DREAMER_AGENT_ID")
    return raw or None


def _row_to_dream(row: asyncpg.Record) -> Dream:
    sf = row["session_filter"]
    if isinstance(sf, str):
        sf = json.loads(sf) if sf else {}
    return Dream(
        id=str(row["id"]),
        input_memory_store_id=str(row["input_memory_store_id"])
        if row["input_memory_store_id"] is not None else None,
        output_memory_store_id=str(row["output_memory_store_id"])
        if row["output_memory_store_id"] is not None else None,
        output_store_name=row["output_store_name"],
        dreamer_session_id=str(row["dreamer_session_id"])
        if row["dreamer_session_id"] is not None else None,
        dreamer_agent_id=str(row["dreamer_agent_id"])
        if row["dreamer_agent_id"] is not None else None,
        session_filter=DreamSessionFilter.model_validate(sf or {}),
        status=row["status"],
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
    )


def _parse_uuid(value: str, label: str = "Dream") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"{label} {value} not found"},
        )


@router.post("", response_model=Dream, status_code=201)
async def start_dream(body: CreateDreamRequest) -> Dream:
    """Kick off a Dream.

    Validates the input memory store exists + isn't archived, that
    the dreamer agent is configured (per-request override or platform
    default), and that the output store name isn't already in use by
    a live store in the workspace. Inserts the row with
    ``status='pending'`` — a background runner picks it up. (For
    v0.7 the actual runner is the same orchestrator loop sessions
    use; this PR ships the API + status surface, the runner-fanout is
    operator-driven.)
    """
    try:
        input_uid = uuid.UUID(body.input_memory_store_id)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": f"Invalid input_memory_store_id: {body.input_memory_store_id}",
            },
        )
    input_row = await fetch_one(
        "SELECT id, archived_at FROM memory_stores WHERE id = $1",
        input_uid,
    )
    if input_row is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": f"input memory store {body.input_memory_store_id} not found",
            },
        )
    if input_row["archived_at"] is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": (
                    f"input memory store {body.input_memory_store_id} is archived"
                ),
            },
        )

    dreamer_id_raw = body.dreamer_agent_id or default_dreamer_agent_id()
    if not dreamer_id_raw:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "dreamer_not_configured",
                "message": (
                    "no dreamer agent configured: set LINCHPIN_DREAMER_AGENT_ID "
                    "or pass dreamer_agent_id on the request"
                ),
            },
        )
    try:
        dreamer_uid = uuid.UUID(dreamer_id_raw)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": f"Invalid dreamer_agent_id: {dreamer_id_raw}",
            },
        )
    agent_row = await fetch_one(
        "SELECT id, archived_at FROM agents WHERE id = $1",
        dreamer_uid,
    )
    if agent_row is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": f"dreamer agent {dreamer_id_raw} not found",
            },
        )
    if agent_row["archived_at"] is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": f"dreamer agent {dreamer_id_raw} is archived",
            },
        )

    # Reject when the output name is already taken by a live store
    # in the workspace — the partial unique index on memory_stores
    # would catch this at write time anyway, but a clean 409 is a
    # better caller experience.
    existing_out = await fetch_one(
        "SELECT id FROM memory_stores WHERE workspace_id = $1 AND name = $2 "
        "AND archived_at IS NULL",
        _DEFAULT_WORKSPACE,
        body.output_store_name,
    )
    if existing_out is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "output_store_name_taken",
                "message": (
                    f"a live memory_store named {body.output_store_name!r} "
                    "already exists in this workspace"
                ),
            },
        )

    dream_id = uuid.uuid4()
    row = await fetch_one(
        """
        INSERT INTO dreams
            (id, workspace_id, input_memory_store_id, output_store_name,
             dreamer_agent_id, session_filter, status)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'pending')
        RETURNING *
        """,
        dream_id,
        _DEFAULT_WORKSPACE,
        input_uid,
        body.output_store_name,
        dreamer_uid,
        body.session_filter.model_dump_json(),
    )
    return _row_to_dream(row)


@router.get("", response_model=DreamListResponse)
async def list_dreams(
    status: DreamStatus | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> DreamListResponse:
    """List Dreams in the workspace, newest first. Optional ``status``
    filter."""
    if status is not None:
        rows = await fetch_all(
            "SELECT * FROM dreams WHERE workspace_id = $1 AND status = $2 "
            "ORDER BY created_at DESC LIMIT $3 OFFSET $4",
            _DEFAULT_WORKSPACE, status, limit + 1, offset,
        )
    else:
        rows = await fetch_all(
            "SELECT * FROM dreams WHERE workspace_id = $1 "
            "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            _DEFAULT_WORKSPACE, limit + 1, offset,
        )
    has_more = len(rows) > limit
    items = rows[:limit]
    return DreamListResponse(
        data=[_row_to_dream(r) for r in items],
        has_more=has_more,
        next_cursor=None,
    )


@router.get("/{dream_id}", response_model=Dream)
async def get_dream(dream_id: str) -> Dream:
    uid = _parse_uuid(dream_id)
    row = await fetch_one("SELECT * FROM dreams WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Dream {dream_id} not found"},
        )
    return _row_to_dream(row)


@router.post("/{dream_id}/cancel", response_model=Dream)
async def cancel_dream(dream_id: str) -> Dream:
    """Cancel a pending or running Dream. Terminal Dreams (``completed``
    / ``failed`` / ``canceled``) return 409 — there's nothing to cancel.

    Also terminates the dreamer's underlying session if one exists, so
    the orchestrator loop stops and the container is reclaimed. Best-
    effort: a failure to terminate the session doesn't block the
    Dream-row cancel.
    """
    uid = _parse_uuid(dream_id)
    row = await fetch_one("SELECT * FROM dreams WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Dream {dream_id} not found"},
        )
    if row["status"] in DREAM_TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "conflict",
                "message": f"Dream {dream_id} is already {row['status']}",
            },
        )
    now = datetime.now(tz=timezone.utc)
    updated = await fetch_one(
        "UPDATE dreams SET status = 'canceled', ended_at = $1 "
        "WHERE id = $2 RETURNING *",
        now, uid,
    )

    # Best-effort terminate the dreamer's session — the row already
    # flipped to ``canceled`` so a failure here is logged, not raised.
    if row["dreamer_session_id"] is not None:
        try:
            await execute(
                "UPDATE sessions SET status = 'terminated', updated_at = NOW() "
                "WHERE id = $1 AND status NOT IN ('terminated', 'failed')",
                row["dreamer_session_id"],
            )
        except Exception:
            logger.exception(
                "failed to terminate dreamer session %s on dream cancel",
                row["dreamer_session_id"],
            )
    return _row_to_dream(updated)
