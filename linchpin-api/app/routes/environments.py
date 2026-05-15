"""Environment CRUD endpoints.

POST   /v1/environments              — Create environment
GET    /v1/environments/{id}         — Get environment by id
GET    /v1/environments              — List environments (paginated; ?include_archived=true to see archived)
POST   /v1/environments/{id}/archive — Soft-delete (set archived_at) — v0.2.0 item #6
DELETE /v1/environments/{id}         — Hard-delete (409 if sessions still reference it) — v0.2.0 item #6

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Query

from app.db import execute, fetch_all, fetch_one
from app.models import (
    CreateEnvironmentRequest,
    EnvironmentResponse,
    PaginatedListResponse,
)

router = APIRouter(prefix="/environments", tags=["environments"])


def _row_to_environment(row) -> EnvironmentResponse:
    """Convert an asyncpg Record to an EnvironmentResponse."""
    return EnvironmentResponse(
        id=str(row["id"]),
        name=row["name"],
        config=json.loads(row["config"]) if isinstance(row["config"], str) else row["config"],
        created_at=row["created_at"],
        archived_at=row["archived_at"] if "archived_at" in row.keys() else None,
    )


def _parse_env_uuid(environment_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(environment_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Environment {environment_id} not found"},
        )


@router.post("", status_code=201, response_model=EnvironmentResponse)
async def create_environment(body: CreateEnvironmentRequest) -> EnvironmentResponse:
    """Create a new environment resource."""
    env_id = str(uuid.uuid4())

    config_json = body.config.model_dump(mode="json")

    row = await fetch_one(
        """
        INSERT INTO environments (id, name, config)
        VALUES ($1, $2, $3::jsonb)
        RETURNING *
        """,
        uuid.UUID(env_id),
        body.name,
        json.dumps(config_json),
    )

    return _row_to_environment(row)


@router.get("/{environment_id}", response_model=EnvironmentResponse)
async def get_environment(environment_id: str) -> EnvironmentResponse:
    """Retrieve an environment by id. Returns archived envs too — callers
    that need to filter can check ``archived_at`` on the response."""
    uid = _parse_env_uuid(environment_id)

    row = await fetch_one("SELECT * FROM environments WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Environment {environment_id} not found"},
        )
    return _row_to_environment(row)


@router.get("", response_model=PaginatedListResponse)
async def list_environments(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_archived: bool = Query(default=False),
) -> PaginatedListResponse:
    """Return a paginated list of environments. Archived envs are excluded
    by default — opt in with ``?include_archived=true``."""
    if include_archived:
        rows = await fetch_all(
            "SELECT * FROM environments ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            limit + 1,
            offset,
        )
    else:
        rows = await fetch_all(
            """SELECT * FROM environments
               WHERE archived_at IS NULL
               ORDER BY created_at DESC
               LIMIT $1 OFFSET $2""",
            limit + 1,
            offset,
        )

    has_more = len(rows) > limit
    items = rows[:limit]

    environments = [_row_to_environment(r) for r in items]

    return PaginatedListResponse(
        data=environments,
        has_more=has_more,
        next_cursor=None,
    )


@router.post("/{environment_id}/archive", response_model=EnvironmentResponse)
async def archive_environment(environment_id: str) -> EnvironmentResponse:
    """Soft-delete an environment (v0.2.0 item #6).

    Idempotent — archiving an already-archived env returns the same row
    without bumping ``archived_at`` again. Existing sessions retain their
    ``environment_id`` FK; the env stays reachable via GET for forensic and
    replay purposes.
    """
    uid = _parse_env_uuid(environment_id)

    existing = await fetch_one("SELECT * FROM environments WHERE id = $1", uid)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Environment {environment_id} not found"},
        )
    if existing["archived_at"] is not None:
        # Idempotent: already archived → return as-is.
        return _row_to_environment(existing)

    row = await fetch_one(
        """UPDATE environments
              SET archived_at = now()
            WHERE id = $1
        RETURNING *""",
        uid,
    )
    return _row_to_environment(row)


@router.delete("/{environment_id}", status_code=204)
async def delete_environment(environment_id: str):
    """Hard-delete an environment (v0.2.0 item #6).

    Rejects 409 if any non-terminated session still references this env —
    forcing the caller to terminate dependent sessions first. Archive is the
    soft-delete path; this endpoint is for permanent removal.
    """
    uid = _parse_env_uuid(environment_id)

    existing = await fetch_one("SELECT id FROM environments WHERE id = $1", uid)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Environment {environment_id} not found"},
        )

    # Reject deletion while non-terminated sessions reference this env. The
    # FK doesn't cascade; deletion would otherwise leave orphan rows.
    in_use = await fetch_one(
        """SELECT id, status FROM sessions
           WHERE environment_id = $1 AND status != 'terminated'
           LIMIT 1""",
        uid,
    )
    if in_use is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "environment_in_use",
                "message": (
                    f"Environment {environment_id} has at least one non-terminated session "
                    f"({in_use['id']}, status={in_use['status']!r}); terminate dependent "
                    "sessions or archive the environment instead."
                ),
                "blocking_session_id": str(in_use["id"]),
                "blocking_session_status": in_use["status"],
            },
        )

    await execute("DELETE FROM environments WHERE id = $1", uid)
