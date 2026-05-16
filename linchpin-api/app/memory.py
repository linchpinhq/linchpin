"""Memory subsystem helpers (v0.3.0).

The ``MemoryWriter`` class is the single canonical path for writes —
called from both the HTTP API (``app/routes/memories.py``) and the
in-sandbox watcher (lands in PR4). Centralizing version creation here
means both paths share identical seq/sha/precondition semantics.

Storage uses a dedicated ``LocalFileStore`` instance rooted at
``LINCHPIN_MEMORY_ROOT`` — independent from the Files API root so
operators can mount them on different volumes and a memory deletion
can't reach into the Files API's content-addressed layout.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.db import execute, fetch_one
from app.files import FileStore, LocalFileStore
from app.models import (
    MEMORY_MAX_BYTES_PER_MEMORY,
    ContentShaPrecondition,
    Memory,
    MemoryVersionAction,
    validate_memory_path,
)


def memory_retention_days() -> int:
    """Configurable retention; hard-capped at 365 by env-var policy."""
    raw = os.environ.get("LINCHPIN_MEMORY_VERSION_RETENTION_DAYS", "30")
    try:
        days = int(raw)
    except ValueError:
        return 30
    if days < 1:
        return 1
    if days > 365:
        return 365
    return days


def memory_store_root() -> str:
    """Root directory for memory bytes — independent of the Files API
    root so operators can mount them on different volumes."""
    return os.environ.get(
        "LINCHPIN_MEMORY_ROOT", "/var/lib/linchpin/memory_stores"
    )


@dataclass(frozen=True, slots=True)
class WriteResult:
    """What ``MemoryWriter.write`` returns to its callers."""

    memory: Memory
    version_id: str
    seq: int
    action: MemoryVersionAction


class PreconditionFailed(Exception):
    """Raised when a write precondition (sha256 match) doesn't match
    the current head. Carries the actual head sha so the caller can
    surface it in the 412 body without an extra query.
    """

    def __init__(self, *, expected: str | None, actual: str | None) -> None:
        super().__init__(
            f"precondition failed: expected={expected!r} actual={actual!r}"
        )
        self.expected = expected
        self.actual = actual


class MemoryWriter:
    """Canonical write path. Used by routes + watcher (PR4)."""

    def __init__(self, *, file_store: FileStore | None = None) -> None:
        self._fs = file_store

    def file_store(self) -> FileStore:
        """Return the per-process memory FileStore — a separate
        ``LocalFileStore`` rooted at ``LINCHPIN_MEMORY_ROOT`` so memory
        bytes never share a tree with Files-API uploads.
        """
        if self._fs is None:
            self._fs = LocalFileStore(root=memory_store_root())
        return self._fs

    async def write(
        self,
        *,
        memory_store_id: uuid.UUID,
        path: str,
        content: bytes,
        author: str,
        precondition: ContentShaPrecondition | None = None,
    ) -> WriteResult:
        """Create or update a memory at *path*. Always produces a new
        version row; never overwrites bytes in place."""
        path = validate_memory_path(path)
        if len(content) > MEMORY_MAX_BYTES_PER_MEMORY:
            raise ValueError(
                f"content size {len(content)} exceeds cap "
                f"{MEMORY_MAX_BYTES_PER_MEMORY}"
            )

        sha = hashlib.sha256(content).hexdigest()
        size_bytes = len(content)

        # Find existing memory + check precondition.
        existing = await fetch_one(
            """
            SELECT id, content_sha256, created_at
            FROM memories
            WHERE memory_store_id = $1 AND path = $2 AND deleted_at IS NULL
            """,
            memory_store_id,
            path,
        )
        actual_head_sha = existing["content_sha256"] if existing else None
        if precondition is not None:
            if precondition.sha256 != actual_head_sha:
                raise PreconditionFailed(
                    expected=precondition.sha256, actual=actual_head_sha
                )

        action: MemoryVersionAction = "update" if existing else "create"

        # Persist bytes under the memstore/ namespace.
        storage_path = await self._persist_bytes(sha, content)

        # Upsert the memory row.
        now = datetime.now(tz=timezone.utc)
        if existing is None:
            memory_id = uuid.uuid4()
            await execute(
                """
                INSERT INTO memories
                    (id, memory_store_id, path, content_sha256, size_bytes,
                     created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $6)
                """,
                memory_id,
                memory_store_id,
                path,
                sha,
                size_bytes,
                now,
            )
        else:
            memory_id = existing["id"]
            await execute(
                """
                UPDATE memories
                SET content_sha256 = $1, size_bytes = $2, updated_at = $3
                WHERE id = $4
                """,
                sha,
                size_bytes,
                now,
                memory_id,
            )

        seq = await self._next_seq(memory_id)
        version_id = uuid.uuid4()
        expires_at = now + timedelta(days=memory_retention_days())
        await execute(
            """
            INSERT INTO memory_versions
                (id, memory_id, memory_store_id, seq, content_sha256, size_bytes,
                 storage_path, author, action, created_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            version_id,
            memory_id,
            memory_store_id,
            seq,
            sha,
            size_bytes,
            storage_path,
            author,
            action,
            now,
            expires_at,
        )

        memory = Memory(
            id=str(memory_id),
            memory_store_id=str(memory_store_id),
            path=path,
            content_sha256=sha,
            size_bytes=size_bytes,
            created_at=existing["created_at"] if existing is not None else now,
            updated_at=now,
        )
        return WriteResult(
            memory=memory, version_id=str(version_id), seq=seq, action=action
        )

    async def delete(
        self,
        *,
        memory_store_id: uuid.UUID,
        path: str,
        author: str,
    ) -> str | None:
        """Soft-delete a memory; write a zero-byte ``action='delete'``
        version. Returns the version id, or None if the memory wasn't
        present.
        """
        path = validate_memory_path(path)
        existing = await fetch_one(
            """
            SELECT id FROM memories
            WHERE memory_store_id = $1 AND path = $2 AND deleted_at IS NULL
            """,
            memory_store_id,
            path,
        )
        if existing is None:
            return None
        memory_id = existing["id"]
        now = datetime.now(tz=timezone.utc)
        await execute(
            "UPDATE memories SET deleted_at = $1 WHERE id = $2",
            now,
            memory_id,
        )
        seq = await self._next_seq(memory_id)
        version_id = uuid.uuid4()
        expires_at = now + timedelta(days=memory_retention_days())
        # Tombstone-shaped version row: sha zeros, zero bytes, empty storage.
        await execute(
            """
            INSERT INTO memory_versions
                (id, memory_id, memory_store_id, seq, content_sha256, size_bytes,
                 storage_path, author, action, created_at, expires_at)
            VALUES ($1, $2, $3, $4, $5, 0, '', $6, 'delete', $7, $8)
            """,
            version_id,
            memory_id,
            memory_store_id,
            seq,
            "0" * 64,
            author,
            now,
            expires_at,
        )
        return str(version_id)

    async def _next_seq(self, memory_id: uuid.UUID) -> int:
        row = await fetch_one(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM memory_versions WHERE memory_id = $1",
            memory_id,
        )
        return row["next"]

    async def _persist_bytes(self, sha: str, content: bytes) -> str:
        """Write content via the memory FileStore. Content-addressable
        layout: ``<sha[:2]>/<sha[2:4]>/<sha>`` under the memory root.

        Returns the relative storage_path that ``memory_versions`` rows
        record. Two writes of identical bytes share the same on-disk
        file thanks to the FileStore's content-addressed layout.
        """
        fs = self.file_store()

        async def _one_chunk() -> AsyncIterator[bytes]:
            yield content

        storage_path, _digest, _size = await fs.write(
            _one_chunk(), max_bytes=MEMORY_MAX_BYTES_PER_MEMORY
        )
        # _digest matches sha by construction; cap was enforced before this call.
        return storage_path


# Shared singleton — the per-process app uses one writer instance.
_writer: MemoryWriter | None = None


async def get_memory_writer() -> MemoryWriter:
    global _writer
    if _writer is None:
        _writer = MemoryWriter()
    return _writer


def precondition_failed_response(exc: PreconditionFailed) -> HTTPException:
    """Translate a PreconditionFailed into the API's 412 shape."""
    return HTTPException(
        status_code=412,
        detail={
            "error": "precondition_failed",
            "expected_sha256": exc.expected,
            "actual_sha256": exc.actual,
        },
    )
