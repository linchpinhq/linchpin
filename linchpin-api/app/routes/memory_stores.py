"""Memory store CRUD endpoints (v0.3.0).

POST   /v1/memory_stores                 — create
GET    /v1/memory_stores                 — list (paginated; ?include_archived)
GET    /v1/memory_stores/{id}            — get
PATCH  /v1/memory_stores/{id}            — update description
POST   /v1/memory_stores/{id}/archive    — soft-delete (idempotent)
DELETE /v1/memory_stores/{id}            — hard-delete (409 if any active session has it mounted)
"""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, HTTPException, Query

from app.db import execute, fetch_all, fetch_one
from app.models import (
    CreateMemoryStoreRequest,
    MemoryStore,
    PaginatedListResponse,
    UpdateMemoryStoreRequest,
)


router = APIRouter(prefix="/memory_stores", tags=["memory"])


def _workspace_id() -> uuid.UUID:
    """Single-tenant for v0.3 — every store carries this constant
    workspace_id until multi-tenancy lands post-v1.0."""
    raw = os.environ.get(
        "LINCHPIN_DEFAULT_WORKSPACE_ID",
        "00000000-0000-0000-0000-000000000001",
    )
    try:
        return uuid.UUID(raw)
    except ValueError:
        return uuid.UUID("00000000-0000-0000-0000-000000000001")


def _row_to_store(row) -> MemoryStore:
    return MemoryStore(
        id=str(row["id"]),
        name=row["name"],
        description=row["description"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def _parse_store_uuid(store_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(store_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Memory store {store_id} not found",
            },
        )


@router.post("", status_code=201, response_model=MemoryStore)
async def create_memory_store(body: CreateMemoryStoreRequest) -> MemoryStore:
    """Create a memory store. ``name`` must be unique (live) within the
    workspace — soft-deleted stores don't count toward uniqueness."""
    store_id = uuid.uuid4()
    workspace_id = _workspace_id()

    # Uniqueness check (partial index would also catch this, but a clean
    # 409 is friendlier than a 500 with a UniqueViolation).
    existing = await fetch_one(
        """
        SELECT id FROM memory_stores
        WHERE workspace_id = $1 AND name = $2 AND archived_at IS NULL
        """,
        workspace_id,
        body.name,
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "name_conflict",
                "message": f"Memory store named {body.name!r} already exists",
            },
        )

    row = await fetch_one(
        """
        INSERT INTO memory_stores (id, name, description, workspace_id)
        VALUES ($1, $2, $3, $4)
        RETURNING *
        """,
        store_id,
        body.name,
        body.description,
        workspace_id,
    )
    return _row_to_store(row)


@router.get("/{store_id}", response_model=MemoryStore)
async def get_memory_store(store_id: str) -> MemoryStore:
    uid = _parse_store_uuid(store_id)
    row = await fetch_one("SELECT * FROM memory_stores WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Memory store {store_id} not found",
            },
        )
    return _row_to_store(row)


@router.patch("/{store_id}", response_model=MemoryStore)
async def update_memory_store(
    store_id: str, body: UpdateMemoryStoreRequest
) -> MemoryStore:
    uid = _parse_store_uuid(store_id)
    existing = await fetch_one(
        "SELECT * FROM memory_stores WHERE id = $1", uid
    )
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Memory store {store_id} not found",
            },
        )
    if body.description is None:
        return _row_to_store(existing)
    row = await fetch_one(
        """
        UPDATE memory_stores
        SET description = $1, updated_at = NOW()
        WHERE id = $2
        RETURNING *
        """,
        body.description,
        uid,
    )
    return _row_to_store(row)


@router.post("/{store_id}/archive", response_model=MemoryStore)
async def archive_memory_store(store_id: str) -> MemoryStore:
    uid = _parse_store_uuid(store_id)
    existing = await fetch_one(
        "SELECT * FROM memory_stores WHERE id = $1", uid
    )
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Memory store {store_id} not found",
            },
        )
    if existing["archived_at"] is not None:
        return _row_to_store(existing)
    row = await fetch_one(
        """
        UPDATE memory_stores SET archived_at = NOW(), updated_at = NOW()
        WHERE id = $1 RETURNING *
        """,
        uid,
    )
    return _row_to_store(row)


@router.delete("/{store_id}", status_code=204)
async def delete_memory_store(store_id: str) -> None:
    """Hard delete: cascades to memories + versions. Refuses (409) if any
    active session references this store via ``session_resources``.
    """
    uid = _parse_store_uuid(store_id)
    existing = await fetch_one(
        "SELECT id FROM memory_stores WHERE id = $1", uid
    )
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Memory store {store_id} not found",
            },
        )

    # Block deletion while a running session has this store mounted.
    conflict = await fetch_one(
        """
        SELECT sr.session_id, s.status
        FROM session_resources sr
        JOIN sessions s ON s.id = sr.session_id
        WHERE sr.type = 'memory_store'
          AND sr.config->>'memory_store_id' = $1
          AND sr.state IN ('mounted','unmounting')
          AND s.status = 'running'
        LIMIT 1
        """,
        str(uid),
    )
    if conflict is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "memory_store_in_use",
                "blocking_session_id": str(conflict["session_id"]),
                "blocking_session_status": conflict["status"],
            },
        )

    await execute("DELETE FROM memory_stores WHERE id = $1", uid)


@router.get("", response_model=PaginatedListResponse)
async def list_memory_stores(
    limit: int = Query(20, ge=1, le=100),
    include_archived: bool = Query(False),
) -> PaginatedListResponse:
    where = "WHERE workspace_id = $1"
    if not include_archived:
        where += " AND archived_at IS NULL"
    rows = await fetch_all(
        f"SELECT * FROM memory_stores {where} ORDER BY created_at DESC LIMIT $2",
        _workspace_id(),
        limit,
    )
    return PaginatedListResponse(
        data=[_row_to_store(r) for r in rows],
        next_cursor=None,
    )
