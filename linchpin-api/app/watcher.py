"""Deliverables watcher (v0.2.0 PR5).

Per eng-review decisions D1 + D3 + D4 + D11 + D12 from the Foundations Tech Spec:

* **D1** — Python `watchfiles` rather than Go binary; plain bind mount under
  `/mnt/session/outputs/` (no overlay2).
* **D3** — In-process asyncio task per session. Spawned by `routes/sessions.py`
  at session boot; cancelled on terminate.
* **D4** — Restart hardening:
    - Boot scan ingests anything written while the watcher was down.
    - Aggregate cap counter re-seeded from `SELECT SUM(size_bytes)` on start.
    - Files that exceed per-file or aggregate caps get unlinked (`os.unlink`)
      so the host bind doesn't leak disk on rejected writes.
* **D11** — `step=50ms, debounce=200ms`. Default `watchfiles` debounce of
  1.6s is tuned for human editors; agents write atomically so a tighter
  window matches workload reality.
* **D12** — Restart story covered by the boot scan; restart-recovery test
  in `tests/test_watcher.py` is the gate.

The watcher posts directly to the DB via the same connection pool the rest of
`linchpin-api` uses — no internal HTTP loopback needed (we have function-level
access; the spec's `/internal/files` endpoint was Go-binary-specific).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from pathlib import Path

from watchfiles import Change, awatch

from app.db import execute, fetch_one
from app.events import append_event
from app.files import (
    FileTooLargeError,
    FileStore,
    get_file_store,
    get_max_bytes,
    get_per_session_cap_bytes,
    get_session_outputs_root,
)

logger = logging.getLogger("linchpin-api.watcher")


# `watchfiles` events arrive when files are added/modified. We only want to
# ingest a file once it's stopped changing — `awatch`'s debounce already gives
# us that quiescence guarantee on a per-batch basis. We still maintain a
# per-path "in-flight" set so concurrent rename-then-truncate sequences from
# tools like editors don't double-ingest the same logical write.


# Public for tests so they can drop in a fake step/debounce.
DEFAULT_STEP_MS = 50
DEFAULT_DEBOUNCE_MS = 200


def session_outputs_dir(session_id: str) -> Path:
    """Host-side directory the session container bind-mounts at /mnt/session/outputs/."""
    return Path(get_session_outputs_root()) / session_id


def ensure_session_outputs_dir(session_id: str) -> Path:
    """Make + return the per-session outputs dir, idempotent."""
    p = session_outputs_dir(session_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _aggregate_used_bytes(session_id: str) -> int:
    """Total deliverable bytes already registered for ``session_id``.

    Re-seeded on watcher boot to survive api restarts (D4).
    """
    row = await fetch_one(
        """
        SELECT COALESCE(SUM(size_bytes), 0) AS total
          FROM files
         WHERE scope_type = 'session'
           AND scope_id   = $1
           AND archived_at IS NULL
        """,
        uuid.UUID(session_id),
    )
    return int(row["total"]) if row is not None else 0


async def _already_ingested(session_id: str, sha256: str) -> bool:
    """True if this exact content is already a deliverable for the session.

    Lets the boot scan (and any debounce-induced double-fire) be idempotent.
    """
    row = await fetch_one(
        """
        SELECT 1
          FROM files
         WHERE scope_type = 'session'
           AND scope_id   = $1
           AND sha256     = $2
           AND archived_at IS NULL
         LIMIT 1
        """,
        uuid.UUID(session_id),
        sha256,
    )
    return row is not None


def _sha256_of_file(path: str) -> str:
    """Stream-hash a file from disk. Watcher fires post-write so the file
    is small/medium and disk-bound; SHA256 is plenty fast."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def _drop_oversized(host_path: str, *, reason: str, session_id: str, error: str) -> None:
    """Unlink the host bytes (D4 — cleanup-on-drop) and emit a dropped event."""
    try:
        os.unlink(host_path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("failed to unlink dropped deliverable %s", host_path)
    try:
        await append_event(
            session_id,
            "agent.deliverable_dropped",
            {
                "path": host_path,
                "reason": reason,
                "error": error,
            },
        )
    except Exception:
        logger.exception("failed to append agent.deliverable_dropped event")


async def _ingest_one(
    session_id: str,
    host_path: str,
    *,
    store: FileStore,
    per_file_cap: int,
    aggregate_cap: int,
    used_so_far: int,
) -> int:
    """Ingest a single file. Returns the size added (0 if dropped).

    Encodes the cap-and-cleanup contract: per-file cap → drop; aggregate cap
    exceeded → drop; FileStore happy-path → INSERT files row + emit
    `agent.deliverable` event.
    """
    # Re-check the file still exists on disk — watchfiles can race with a
    # quick write-then-delete; if the file is gone, nothing to do.
    if not os.path.isfile(host_path):
        return 0

    try:
        size = os.path.getsize(host_path)
    except OSError:
        return 0

    if size > per_file_cap:
        await _drop_oversized(
            host_path,
            reason="per_file_cap_exceeded",
            session_id=session_id,
            error=f"deliverable size {size}B exceeds per-file cap {per_file_cap}B",
        )
        return 0
    if used_so_far + size > aggregate_cap:
        await _drop_oversized(
            host_path,
            reason="aggregate_cap_exceeded",
            session_id=session_id,
            error=(
                f"deliverable would push session over {aggregate_cap}B aggregate cap "
                f"(used={used_so_far}B, this={size}B)"
            ),
        )
        return 0

    try:
        sha256 = _sha256_of_file(host_path)
    except FileNotFoundError:
        return 0

    if await _already_ingested(session_id, sha256):
        # Boot scan / debounce double-fire: this exact content is already
        # registered. No double-insert, no double-event.
        return 0

    try:
        storage_path, _digest, size_bytes = await store.write_from_path(
            host_path, max_bytes=per_file_cap,
        )
    except FileTooLargeError:
        # Re-raise to drop — shouldn't normally happen since we checked above,
        # but keeps the FileStore contract honest.
        await _drop_oversized(
            host_path,
            reason="per_file_cap_exceeded",
            session_id=session_id,
            error=f"FileStore rejected size > {per_file_cap}B",
        )
        return 0

    file_id = uuid.uuid4()
    filename = os.path.basename(host_path)
    content_type = _guess_content_type(filename)
    await execute(
        """
        INSERT INTO files
            (id, filename, content_type, size_bytes,
             storage_path, sha256, source, downloadable,
             scope_type, scope_id)
        VALUES ($1, $2, $3, $4,
                $5, $6, 'deliverable', TRUE,
                'session', $7)
        ON CONFLICT DO NOTHING
        """,
        file_id,
        filename,
        content_type,
        size_bytes,
        storage_path,
        sha256,
        uuid.UUID(session_id),
    )
    try:
        await append_event(
            session_id,
            "agent.deliverable",
            {
                "file_id": str(file_id),
                "filename": filename,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "content_type": content_type,
            },
        )
    except Exception:
        logger.exception("failed to append agent.deliverable event")
    return size_bytes


def _guess_content_type(filename: str) -> str:
    """Cheap mimetype guess. mimetypes stdlib covers ~95% of agent outputs;
    the long tail falls back to application/octet-stream which is correct."""
    import mimetypes
    guessed, _enc = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


async def _boot_scan(session_id: str, outputs_dir: Path, *, store: FileStore) -> int:
    """Ingest anything already in ``outputs_dir`` on watcher start (D4).

    Returns the total bytes registered. Idempotent via sha256 dedup, so
    running again over the same dir is safe.
    """
    per_file_cap = get_max_bytes()
    aggregate_cap = get_per_session_cap_bytes()
    used = await _aggregate_used_bytes(session_id)

    for root, _dirs, names in os.walk(outputs_dir):
        for name in sorted(names):
            host_path = os.path.join(root, name)
            added = await _ingest_one(
                session_id, host_path,
                store=store,
                per_file_cap=per_file_cap,
                aggregate_cap=aggregate_cap,
                used_so_far=used,
            )
            used += added
    return used


async def watch_session_deliverables(
    session_id: str,
    *,
    outputs_dir: Path | None = None,
    step_ms: int = DEFAULT_STEP_MS,
    debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Asyncio task entry point — one per session.

    Run as ``asyncio.create_task(watch_session_deliverables(session_id))`` from
    the session-boot path. Cancel the task on session terminate; the awatch
    loop unwinds on `asyncio.CancelledError`.

    ``stop_event``, when set, exits the watch loop cleanly — used in tests
    to simulate a graceful watcher restart without raising CancelledError.
    """
    outputs = outputs_dir or ensure_session_outputs_dir(session_id)
    store = get_file_store()
    per_file_cap = get_max_bytes()
    aggregate_cap = get_per_session_cap_bytes()

    # Boot scan first so files written during a watcher outage register.
    used = await _boot_scan(session_id, outputs, store=store)
    logger.info(
        "watcher boot scan complete: session=%s used=%dB", session_id, used,
    )

    try:
        async for changes in awatch(
            str(outputs),
            step=step_ms,
            debounce=debounce_ms,
            stop_event=stop_event,
            recursive=True,
        ):
            for change_type, path in changes:
                # We only care about new/modified files (deletes shouldn't
                # propagate to the Files API — caller deletes are out of band).
                if change_type not in (Change.added, Change.modified):
                    continue
                if not os.path.isfile(path):
                    continue
                added = await _ingest_one(
                    session_id, path,
                    store=store,
                    per_file_cap=per_file_cap,
                    aggregate_cap=aggregate_cap,
                    used_so_far=used,
                )
                used += added
    except asyncio.CancelledError:
        logger.info("watcher cancelled: session=%s", session_id)
        raise
