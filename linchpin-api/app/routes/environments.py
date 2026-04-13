"""Environment CRUD endpoints.

POST /v1/environments       — Create environment
GET  /v1/environments/{id}  — Get environment by id
GET  /v1/environments       — List environments (paginated)

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Query

from app.db import fetch_all, fetch_one
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
    """Retrieve an environment by id."""
    try:
        uid = uuid.UUID(environment_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Environment {environment_id} not found"},
        )

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
) -> PaginatedListResponse:
    """Return a paginated list of environments."""
    rows = await fetch_all(
        "SELECT * FROM environments ORDER BY created_at DESC LIMIT $1 OFFSET $2",
        limit + 1,  # fetch one extra to detect has_more
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
