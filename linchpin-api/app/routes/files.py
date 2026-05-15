"""Files API endpoints (v0.2.0 PR1 + PR3).

POST   /v1/files                       — Upload a file (multipart)
GET    /v1/files                       — List files; ``?scope_id=`` filters by scope
GET    /v1/files/{file_id}             — Get file metadata
GET    /v1/files/{file_id}/content     — Stream file bytes (403 if not downloadable)
DELETE /v1/files/{file_id}             — Hard delete (row + bytes); 409 if mounted

PR1 shipped the user-facing surface and reference-counted dedup delete.
PR3 added the active-mount 409 check (eng-review D10 — owned here after PR4
was deferred to v0.2.x). Deliverable ingestion (``source=deliverable`` via
the in-process watcher) lands in PR5.
"""

from __future__ import annotations

import logging
import urllib.parse
import uuid
from collections.abc import AsyncIterator

import asyncpg
from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse

from app.db import execute, fetch_all, fetch_one
from app.files import FileTooLargeError, get_file_store, get_max_bytes
from app.models import FileResponse, PaginatedFilesResponse

logger = logging.getLogger("linchpin-api")

router = APIRouter(prefix="/files", tags=["files"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_file(row: asyncpg.Record) -> FileResponse:
    return FileResponse(
        id=str(row["id"]),
        filename=row["filename"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        source=row["source"],
        downloadable=row["downloadable"],
        scope_type=row["scope_type"],
        scope_id=str(row["scope_id"]) if row["scope_id"] is not None else None,
        created_at=row["created_at"],
        archived_at=row["archived_at"],
    )


def _parse_uuid(value: str, label: str) -> uuid.UUID:
    """Parse a UUID or raise 404. Used for file_id; rejects malformed input as missing."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"{label} {value} not found"},
        )


async def _upload_file_chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    """Yield chunks from an UploadFile until EOF."""
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            return
        yield chunk


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", response_model=FileResponse, status_code=201)
async def upload_file(file: UploadFile = File(...)) -> FileResponse:
    """Upload a file. Persisted with ``source=upload`` and ``downloadable=false``."""
    store = get_file_store()
    max_bytes = get_max_bytes()

    try:
        storage_path, sha256, size_bytes = await store.write(
            _upload_file_chunks(file),
            max_bytes=max_bytes,
        )
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "file_too_large",
                "message": f"file exceeds {exc.limit_bytes} byte cap",
                "limit_bytes": exc.limit_bytes,
            },
        )

    row = await fetch_one(
        """
        INSERT INTO files (
            filename, content_type, size_bytes, storage_path, sha256,
            source, downloadable, scope_type, scope_id
        )
        VALUES ($1, $2, $3, $4, $5, 'upload', false, NULL, NULL)
        RETURNING *
        """,
        file.filename or "untitled",
        file.content_type or "application/octet-stream",
        size_bytes,
        storage_path,
        sha256,
    )
    return _row_to_file(row)


@router.get("", response_model=PaginatedFilesResponse)
async def list_files(
    scope_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedFilesResponse:
    """List files. ``scope_id`` filters by scope; omit it for unscoped uploads only.

    No prefix match — scope filtering is exact, so a session cannot list
    another session's deliverables.
    """
    if scope_id is None:
        rows = await fetch_all(
            """
            SELECT * FROM files
            WHERE archived_at IS NULL AND scope_type IS NULL
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit + 1,
            offset,
        )
    else:
        scope_uid = _parse_uuid(scope_id, "scope")
        rows = await fetch_all(
            """
            SELECT * FROM files
            WHERE archived_at IS NULL
              AND scope_type = 'session'
              AND scope_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            scope_uid,
            limit + 1,
            offset,
        )

    has_more = len(rows) > limit
    items = rows[:limit]
    return PaginatedFilesResponse(
        data=[_row_to_file(r) for r in items],
        has_more=has_more,
        next_cursor=None,
    )


@router.get("/{file_id}", response_model=FileResponse)
async def get_file(file_id: str) -> FileResponse:
    """Get file metadata."""
    file_uid = _parse_uuid(file_id, "File")
    row = await fetch_one("SELECT * FROM files WHERE id = $1", file_uid)
    if row is None or row["archived_at"] is not None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"File {file_id} not found"},
        )
    return _row_to_file(row)


@router.get("/{file_id}/content")
async def get_file_content(file_id: str) -> StreamingResponse:
    """Stream file bytes. Returns 403 if the file is not marked downloadable."""
    file_uid = _parse_uuid(file_id, "File")
    row = await fetch_one("SELECT * FROM files WHERE id = $1", file_uid)
    if row is None or row["archived_at"] is not None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"File {file_id} not found"},
        )
    if not row["downloadable"]:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "not_downloadable",
                "message": "uploads are not downloadable; only agent-produced deliverables are",
            },
        )

    store = get_file_store()
    try:
        iterator = store.read(row["storage_path"])
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "storage_missing",
                "message": "file row exists but its bytes are missing from storage",
            },
        )

    # RFC 5987 encoding so filenames with spaces / unicode survive transport.
    filename = row["filename"]
    quoted = urllib.parse.quote(filename, safe="")
    disposition = f'attachment; filename="{filename}"; filename*=UTF-8\'\'{quoted}'

    return StreamingResponse(
        iterator,
        media_type=row["content_type"],
        headers={"Content-Disposition": disposition},
    )


@router.delete("/{file_id}", status_code=204)
async def delete_file(file_id: str):
    """Hard-delete a file: removes the DB row and the stored bytes."""
    file_uid = _parse_uuid(file_id, "File")
    row = await fetch_one("SELECT * FROM files WHERE id = $1", file_uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"File {file_id} not found"},
        )

    # PR3 — D10: 409 if this file is currently mounted (or unmounting) into
    # any active session. PR4 (live mount/unmount) was the original home for
    # this check; with PR4 deferred to v0.2.x, PR3 owns it because PR3 is
    # where session_resources actually starts representing real mounts.
    mounted = await fetch_one(
        """SELECT id, session_id, mount_path FROM session_resources
           WHERE config->>'file_id' = $1
             AND state IN ('mounted', 'unmounting')
           LIMIT 1""",
        str(file_uid),
    )
    if mounted is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "file_in_use",
                "message": (
                    f"File {file_id} is currently mounted in session "
                    f"{mounted['session_id']} at {mounted['mount_path']!r}; "
                    "terminate the session (which unmounts all its resources) "
                    "before deleting the file."
                ),
                "session_id": str(mounted["session_id"]),
                "mount_path": mounted["mount_path"],
            },
        )

    await execute("DELETE FROM files WHERE id = $1", file_uid)

    # Storage bytes are content-addressed and may be shared with other rows
    # that uploaded the same content. Only drop bytes when no other live row
    # references the same storage_path.
    still_referenced = await fetch_one(
        "SELECT 1 FROM files WHERE storage_path = $1 LIMIT 1",
        row["storage_path"],
    )
    if still_referenced is None:
        await get_file_store().delete(row["storage_path"])
