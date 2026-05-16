"""Memory version endpoints (v0.3.0 PR2).

Versions are append-only snapshots produced on every write. They're
retained for ``LINCHPIN_MEMORY_VERSION_RETENTION_DAYS`` (30 by default)
and then tombstoned by the GC pass (PR5). The ``redact`` endpoint runs
the same tombstone logic on demand for compliance / GDPR responses.

GET    /v1/memory_stores/{id}/memory_versions           — list (filter by memory_id)
GET    /v1/memory_stores/{id}/memory_versions/{ver}     — get metadata
GET    /v1/memory_stores/{id}/memory_versions/{ver}/content — binary stream (404 once redacted)
POST   /v1/memory_stores/{id}/memory_versions/{ver}/redact   — zero the bytes, advance head if needed
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.db import execute, fetch_all, fetch_one
from app.files import LocalFileStore
from app.memory import memory_retention_days, memory_store_root
from app.models import (
    MemoryVersion,
    PaginatedListResponse,
    RedactRequest,
)


router = APIRouter(
    prefix="/memory_stores/{store_id}/memory_versions",
    tags=["memory"],
)


def _row_to_version(row) -> MemoryVersion:
    return MemoryVersion(
        id=str(row["id"]),
        memory_id=str(row["memory_id"]),
        memory_store_id=str(row["memory_store_id"]),
        seq=row["seq"],
        content_sha256=row["content_sha256"],
        size_bytes=row["size_bytes"],
        author=row["author"],
        action=row["action"],
        redacted_at=row["redacted_at"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _parse_uuid(value: str, *, kind: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"{kind} {value} not found"},
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


@router.get("", response_model=PaginatedListResponse)
async def list_memory_versions(
    store_id: str,
    memory_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> PaginatedListResponse:
    """List versions in this store. Optionally filter to one memory's
    chain via ``?memory_id=<id>`` — typical usage when an operator is
    reconstructing the history of a single path."""
    store_uid = _parse_uuid(store_id, kind="Memory store")
    await _ensure_store_exists(store_uid)

    if memory_id is not None:
        memory_uid = _parse_uuid(memory_id, kind="Memory")
        rows = await fetch_all(
            """
            SELECT * FROM memory_versions
            WHERE memory_store_id = $1 AND memory_id = $2
            ORDER BY created_at DESC, seq DESC
            LIMIT $3
            """,
            store_uid,
            memory_uid,
            limit,
        )
    else:
        rows = await fetch_all(
            """
            SELECT * FROM memory_versions
            WHERE memory_store_id = $1
            ORDER BY created_at DESC, seq DESC
            LIMIT $2
            """,
            store_uid,
            limit,
        )
    return PaginatedListResponse(
        data=[_row_to_version(r) for r in rows],
        next_cursor=None,
    )


@router.get("/{version_id}", response_model=MemoryVersion)
async def get_memory_version(store_id: str, version_id: str) -> MemoryVersion:
    store_uid = _parse_uuid(store_id, kind="Memory store")
    ver_uid = _parse_uuid(version_id, kind="Memory version")
    await _ensure_store_exists(store_uid)
    row = await fetch_one(
        "SELECT * FROM memory_versions WHERE memory_store_id = $1 AND id = $2",
        store_uid,
        ver_uid,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Memory version {version_id} not found",
            },
        )
    return _row_to_version(row)


@router.get("/{version_id}/content")
async def get_memory_version_content(store_id: str, version_id: str):
    """Stream the raw bytes for this version. Returns 404 once the row
    is redacted (``redacted_at IS NOT NULL``). Delete and create-empty
    versions (zero-byte ``action='delete'`` rows) return an empty body
    with 200 so callers can distinguish "no content was written" from
    "the content was redacted".
    """
    store_uid = _parse_uuid(store_id, kind="Memory store")
    ver_uid = _parse_uuid(version_id, kind="Memory version")
    await _ensure_store_exists(store_uid)
    row = await fetch_one(
        "SELECT * FROM memory_versions WHERE memory_store_id = $1 AND id = $2",
        store_uid,
        ver_uid,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Memory version {version_id} not found",
            },
        )
    if row["redacted_at"] is not None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "redacted",
                "message": "Version has been redacted; content is no longer available",
            },
        )
    storage_path = row["storage_path"]
    if not storage_path:
        # Delete-action version — no bytes to return.
        return StreamingResponse(iter([b""]), media_type="application/octet-stream")
    fs = LocalFileStore(root=memory_store_root())
    return StreamingResponse(
        fs.read(storage_path), media_type="application/octet-stream"
    )


@router.post("/{version_id}/redact", response_model=MemoryVersion)
async def redact_memory_version(
    store_id: str, version_id: str, body: RedactRequest
) -> MemoryVersion:
    """Tombstone a specific version: unlink its bytes from the FileStore,
    zero its ``content_sha256``, and set ``redacted_at``. If the version
    is the current head of its memory, also write a NEW
    ``action='redact'`` version so the agent doesn't read previously-
    redacted content on next mount.
    """
    store_uid = _parse_uuid(store_id, kind="Memory store")
    ver_uid = _parse_uuid(version_id, kind="Memory version")
    await _ensure_store_exists(store_uid)

    row = await fetch_one(
        "SELECT * FROM memory_versions WHERE memory_store_id = $1 AND id = $2",
        store_uid,
        ver_uid,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Memory version {version_id} not found",
            },
        )

    if row["redacted_at"] is not None:
        # Idempotent — already redacted.
        return _row_to_version(row)

    # Unlink bytes from the FileStore. The row stays for audit.
    fs = LocalFileStore(root=memory_store_root())
    if row["storage_path"]:
        await fs.delete(row["storage_path"])

    now = datetime.now(tz=timezone.utc)
    redacted = await fetch_one(
        """
        UPDATE memory_versions
        SET redacted_at = $1, storage_path = '', content_sha256 = repeat('0', 64),
            size_bytes = 0
        WHERE id = $2
        RETURNING *
        """,
        now,
        ver_uid,
    )

    # If we just redacted the current head, advance head by writing a
    # new ``action='redact'`` version so subsequent reads can't surface
    # the previous bytes. Determined by: max(seq) for this memory_id
    # == the redacted row's seq.
    memory_id = row["memory_id"]
    head = await fetch_one(
        "SELECT MAX(seq) AS max_seq FROM memory_versions WHERE memory_id = $1",
        memory_id,
    )
    if head and head["max_seq"] == row["seq"]:
        new_seq = head["max_seq"] + 1
        expires_at = now + timedelta(days=memory_retention_days())
        # Mark the underlying memory soft-deleted so the materialized
        # view of the store no longer surfaces the redacted path.
        await execute(
            "UPDATE memories SET deleted_at = $1, updated_at = $1 WHERE id = $2",
            now,
            memory_id,
        )
        await execute(
            """
            INSERT INTO memory_versions
                (id, memory_id, memory_store_id, seq, content_sha256, size_bytes,
                 storage_path, author, action, created_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, 0, '', $6, 'redact', $7, $8)
            """,
            uuid.uuid4(),
            memory_id,
            store_uid,
            new_seq,
            "0" * 64,
            f"api:redact:{body.reason[:64]}",
            now,
            expires_at,
        )

    return _row_to_version(redacted)
