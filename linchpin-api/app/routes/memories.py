"""Memory CRUD endpoints (v0.3.0).

Routes are nested under a memory_store id and address individual memory
entries by their path (e.g. ``/preferences/formatting.md``) — not by
their UUID. Path-as-handle matches Anthropic's wire shape and lets the
sandbox watcher mirror filesystem semantics 1:1.

POST   /v1/memory_stores/{id}/memories                — create (with optional precondition)
PATCH  /v1/memory_stores/{id}/memories                — update (with optional precondition)
GET    /v1/memory_stores/{id}/memories                — list / get-by-path
DELETE /v1/memory_stores/{id}/memories?path=…         — soft-delete (writes 'delete' version)
"""

from __future__ import annotations

import base64
import uuid

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.db import execute, fetch_all, fetch_one
from app.memory import (
    PreconditionFailed,
    get_memory_writer,
    precondition_failed_response,
)
from app.models import (
    Memory,
    PaginatedListResponse,
    WriteMemoryRequest,
    validate_memory_path,
)


router = APIRouter(
    prefix="/memory_stores/{store_id}/memories",
    tags=["memory"],
)


def _row_to_memory(row) -> Memory:
    return Memory(
        id=str(row["id"]),
        memory_store_id=str(row["memory_store_id"]),
        path=row["path"],
        content_sha256=row["content_sha256"],
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _parse_store_uuid(store_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(store_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Memory store {store_id} not found"},
        )


async def _ensure_store_exists(uid: uuid.UUID) -> None:
    row = await fetch_one(
        "SELECT id FROM memory_stores WHERE id = $1 AND archived_at IS NULL",
        uid,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Memory store {uid} not found",
            },
        )


@router.post("", status_code=201, response_model=Memory)
async def create_memory(store_id: str, body: WriteMemoryRequest) -> Memory:
    """Create or upsert a memory. Optional precondition matches HTTP
    ETag semantics: ``sha256=None`` requires the path to not yet exist;
    ``sha256=<hex>`` requires that to be the current head sha. Either
    direction's mismatch returns 412.
    """
    uid = _parse_store_uuid(store_id)
    await _ensure_store_exists(uid)
    writer = await get_memory_writer()
    try:
        result = await writer.write(
            memory_store_id=uid,
            path=body.path,
            content=body.content,
            author="api",
            precondition=body.precondition,
        )
    except PreconditionFailed as exc:
        raise precondition_failed_response(exc) from exc
    return result.memory


@router.patch("", response_model=Memory)
async def patch_memory(store_id: str, body: WriteMemoryRequest) -> Memory:
    """Same as POST. Kept as a separate verb so callers that want PUT-ish
    semantics can pick PATCH; both endpoints share one canonical writer.
    """
    uid = _parse_store_uuid(store_id)
    await _ensure_store_exists(uid)
    writer = await get_memory_writer()
    try:
        result = await writer.write(
            memory_store_id=uid,
            path=body.path,
            content=body.content,
            author="api",
            precondition=body.precondition,
        )
    except PreconditionFailed as exc:
        raise precondition_failed_response(exc) from exc
    return result.memory


@router.get("", response_model=PaginatedListResponse)
async def list_or_get_memory(
    store_id: str,
    path: str | None = Query(default=None),
    path_prefix: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    """If ``?path=…`` is given, returns the single memory at that path
    wrapped in a one-row PaginatedListResponse (or 404). Otherwise lists
    memories, optionally filtered by ``?path_prefix=/preferences/``.
    """
    uid = _parse_store_uuid(store_id)
    await _ensure_store_exists(uid)

    if path is not None:
        validate_memory_path(path)
        row = await fetch_one(
            """
            SELECT * FROM memories
            WHERE memory_store_id = $1 AND path = $2 AND deleted_at IS NULL
            """,
            uid,
            path,
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "not_found",
                    "message": f"Memory at path {path!r} not found",
                },
            )
        return PaginatedListResponse(data=[_row_to_memory(row)], next_cursor=None)

    where = "memory_store_id = $1 AND deleted_at IS NULL"
    params: list = [uid]
    if path_prefix is not None:
        # Light validation: must start with '/' so callers don't accidentally
        # pass a relative substring.
        if not path_prefix.startswith("/"):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_path_prefix",
                    "message": "path_prefix must start with '/'",
                },
            )
        params.append(f"{path_prefix}%")
        where += f" AND path LIKE ${len(params)}"
    params.append(limit)
    rows = await fetch_all(
        f"SELECT * FROM memories WHERE {where} ORDER BY path LIMIT ${len(params)}",
        *params,
    )
    return PaginatedListResponse(
        data=[_row_to_memory(r) for r in rows],
        next_cursor=None,
    )


@router.delete("", status_code=204)
async def delete_memory(store_id: str, path: str = Query(...)) -> None:
    """Soft-delete a memory by path. Writes an ``action='delete'`` version
    so the version chain stays intact for the 30-day retention window."""
    uid = _parse_store_uuid(store_id)
    await _ensure_store_exists(uid)
    validate_memory_path(path)
    writer = await get_memory_writer()
    version_id = await writer.delete(memory_store_id=uid, path=path, author="api")
    if version_id is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Memory at path {path!r} not found",
            },
        )
