"""Per-(session, store) memory watcher (v0.3.0 PR4).

Counterpart to ``app/watcher.py`` (deliverables) but for memory stores:

- Spawned by ``routes/sessions.py`` once per ``read_write`` memory_store
  resource on session boot.
- Watches the host-side cache dir that's bind-mounted into the container
  at ``/mnt/memory/<store_name>/``.
- On any in-cache write the agent makes, computes sha256, enforces the
  per-memory 100 KB cap, and pipes through ``MemoryWriter`` so the row
  + version go in the DB and the canonical FileStore.
- On in-cache deletes, soft-deletes the memory + writes a tombstone
  version (``action='delete'``).
- Over-cap writes are rolled back: the cache file is truncated to the
  previous head's content (or unlinked if there was no prior version)
  and a ``memory.write_rejected`` event is emitted so the agent learns
  its write didn't stick.

``read_only`` stores get no watcher — the container mount is ``ro`` so
writes inside the sandbox fail at the kernel level before any agent
intent reaches us. PR4 doesn't try to fight that.

The watcher reuses ``watchfiles.awatch`` (same lib as the deliverables
watcher) for cross-platform fs events, with the same tight 50/200 ms
debounce — agents write atomically so we don't need editor-tuned
quiescence windows.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
import uuid
from pathlib import Path

from watchfiles import Change, awatch

from app.db import fetch_one
from app.events import append_event
from app.memory import (
    MemoryWriter,
    PreconditionFailed,
    get_memory_writer,
    session_memory_cache_dir,
)
from app.models import MEMORY_MAX_BYTES_PER_MEMORY

logger = logging.getLogger("linchpin-api.memory-watcher")


# Match the deliverables watcher's debounce so agent-driven writes
# (which are atomic) flush within a couple hundred ms instead of the
# 1.6 s default that's tuned for human editors.
DEFAULT_STEP_MS = 50
DEFAULT_DEBOUNCE_MS = 200


def _container_path_to_memory_path(
    container_relpath: str,
) -> str:
    """Translate ``preferences/formatting.md`` (relative to the cache
    dir) into the canonical memory path ``/preferences/formatting.md``
    that ``MemoryWriter`` and the memories table want.

    The bind mount inside the container is ``/mnt/memory/<store>/`` and
    everything under it is mirrored verbatim into the memory tree, so
    the translation is just "prepend a leading slash and normalize the
    POSIX separator".
    """
    posix = container_relpath.replace(os.sep, "/")
    if not posix.startswith("/"):
        posix = "/" + posix
    return posix


def _sha256_of_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _emit_write_event(
    *,
    session_id: str,
    memory_store_id: uuid.UUID,
    memory_id: str,
    seq: int,
    author: str,
    action: str,
    path: str,
) -> None:
    try:
        await append_event(
            session_id,
            "memory.write",
            {
                "memory_store_id": str(memory_store_id),
                "memory_id": memory_id,
                "seq": seq,
                "author": author,
                "action": action,
                "path": path,
            },
        )
    except Exception:
        logger.exception("failed to append memory.write event")


async def _emit_rejected_event(
    *,
    session_id: str,
    memory_store_id: uuid.UUID,
    path: str,
    reason: str,
    size_bytes: int | None,
) -> None:
    try:
        await append_event(
            session_id,
            "memory.write_rejected",
            {
                "memory_store_id": str(memory_store_id),
                "path": path,
                "reason": reason,
                "size_bytes": size_bytes,
            },
        )
    except Exception:
        logger.exception("failed to append memory.write_rejected event")


async def _rollback_over_cap(
    *,
    cache_path: Path,
    memory_store_id: uuid.UUID,
    memory_path: str,
) -> None:
    """Restore the cache file to the prior head's bytes (or unlink it
    if there was no prior). Keeps the sandbox view consistent with the
    canonical DB state after we reject an over-cap write."""
    prior = await fetch_one(
        """
        SELECT content_sha256
        FROM memories
        WHERE memory_store_id = $1 AND path = $2 AND deleted_at IS NULL
        """,
        memory_store_id,
        memory_path,
    )
    if prior is None:
        try:
            await asyncio.to_thread(cache_path.unlink, missing_ok=True)
        except OSError:
            logger.exception("rollback unlink failed for %s", cache_path)
        return

    sha = prior["content_sha256"]
    writer = await get_memory_writer()
    fs = writer.file_store()
    storage_path = f"{sha[:2]}/{sha[2:4]}/{sha}"
    try:
        def _restore() -> None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "wb") as out:
                for chunk in fs.read(storage_path):
                    out.write(chunk)
        await asyncio.to_thread(_restore)
    except FileNotFoundError:
        # FileStore row vanished underneath us — leave a zero-byte
        # placeholder so the sandbox still sees a file at the path,
        # but the canonical state in DB stays the source of truth.
        try:
            await asyncio.to_thread(cache_path.write_bytes, b"")
        except OSError:
            logger.exception("zero-byte fallback write failed for %s", cache_path)


async def _ingest_one_write(
    *,
    cache_dir: Path,
    cache_path: Path,
    session_id: str,
    memory_store_id: uuid.UUID,
    writer: MemoryWriter,
) -> None:
    """Pipe a single write event through the MemoryWriter."""
    try:
        st = await asyncio.to_thread(os.lstat, str(cache_path))
    except FileNotFoundError:
        return
    except OSError:
        return

    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        # Refuse symlinks and non-regular files. Same exfiltration guard
        # the deliverables watcher uses — a write into the rw bind mount
        # is host-readable; following a symlink lets the agent rope in
        # arbitrary host paths.
        try:
            await asyncio.to_thread(os.unlink, str(cache_path))
        except OSError:
            pass
        return

    try:
        relpath = cache_path.relative_to(cache_dir).as_posix()
    except ValueError:
        return
    memory_path = _container_path_to_memory_path(relpath)

    if st.st_size > MEMORY_MAX_BYTES_PER_MEMORY:
        await _emit_rejected_event(
            session_id=session_id,
            memory_store_id=memory_store_id,
            path=memory_path,
            reason="over_cap",
            size_bytes=st.st_size,
        )
        await _rollback_over_cap(
            cache_path=cache_path,
            memory_store_id=memory_store_id,
            memory_path=memory_path,
        )
        return

    try:
        content = await asyncio.to_thread(cache_path.read_bytes)
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("failed to read memory cache file %s", cache_path)
        return

    # Idempotency: skip if the file's current bytes already match the
    # DB head sha. Boot-scan double-fires + watchfiles debounce overlap
    # both rely on this.
    new_sha = _sha256_of_bytes(content)
    head = await fetch_one(
        """
        SELECT content_sha256 FROM memories
        WHERE memory_store_id = $1 AND path = $2 AND deleted_at IS NULL
        """,
        memory_store_id,
        memory_path,
    )
    if head is not None and head["content_sha256"] == new_sha:
        return

    try:
        result = await writer.write(
            memory_store_id=memory_store_id,
            path=memory_path,
            content=content,
            author=f"session:{session_id}",
        )
    except PreconditionFailed:
        # Watcher writes don't supply preconditions, so this branch only
        # trips on a concurrent racy writer winning the lock — safe to
        # ignore; the loser's bytes are stale.
        return
    except ValueError as exc:
        # MemoryWriter raises ValueError on its own size check too.
        await _emit_rejected_event(
            session_id=session_id,
            memory_store_id=memory_store_id,
            path=memory_path,
            reason="over_cap",
            size_bytes=len(content),
        )
        logger.info("memory write rejected for %s: %s", memory_path, exc)
        return

    await _emit_write_event(
        session_id=session_id,
        memory_store_id=memory_store_id,
        memory_id=result.memory.id,
        seq=result.seq,
        author=f"session:{session_id}",
        action=result.action,
        path=memory_path,
    )


async def _ingest_one_delete(
    *,
    cache_dir: Path,
    cache_path: Path,
    session_id: str,
    memory_store_id: uuid.UUID,
    writer: MemoryWriter,
) -> None:
    try:
        relpath = cache_path.relative_to(cache_dir).as_posix()
    except ValueError:
        return
    memory_path = _container_path_to_memory_path(relpath)

    version_id = await writer.delete(
        memory_store_id=memory_store_id,
        path=memory_path,
        author=f"session:{session_id}",
    )
    if version_id is None:
        return
    # Re-fetch the memory id so the event payload matches write events.
    row = await fetch_one(
        """
        SELECT id FROM memories
        WHERE memory_store_id = $1 AND path = $2
        ORDER BY updated_at DESC LIMIT 1
        """,
        memory_store_id,
        memory_path,
    )
    memory_id = str(row["id"]) if row is not None else ""
    # delete writes a new seq via MemoryWriter; fetch it for the event.
    seq_row = await fetch_one(
        "SELECT MAX(seq) AS seq FROM memory_versions WHERE memory_id = $1",
        row["id"] if row is not None else uuid.UUID(int=0),
    )
    seq = int(seq_row["seq"]) if seq_row and seq_row["seq"] is not None else 0
    await _emit_write_event(
        session_id=session_id,
        memory_store_id=memory_store_id,
        memory_id=memory_id,
        seq=seq,
        author=f"session:{session_id}",
        action="delete",
        path=memory_path,
    )


async def _boot_scan(
    *,
    cache_dir: Path,
    session_id: str,
    memory_store_id: uuid.UUID,
    writer: MemoryWriter,
) -> None:
    """Reconcile the cache dir against DB head shas on watcher start.

    Any file that exists on disk but doesn't match the DB head sha is
    treated as a new write (PR4 path); any DB-tracked memory whose
    backing file is missing is treated as a delete. This is what makes
    the watcher restart-safe — an api restart in the middle of an agent
    write session catches up on the next boot.
    """
    if not cache_dir.exists():
        return

    rows = await fetch_one(
        "SELECT COUNT(*) AS c FROM memories WHERE memory_store_id = $1 AND deleted_at IS NULL",
        memory_store_id,
    )
    # If the store is empty in DB AND the cache dir is empty, fast-path out.
    if (rows is None or int(rows["c"]) == 0):
        try:
            empty = not any(cache_dir.rglob("*"))
        except OSError:
            empty = False
        if empty:
            return

    def _walk() -> list[Path]:
        out: list[Path] = []
        for root, _dirs, names in os.walk(cache_dir):
            for name in sorted(names):
                out.append(Path(root) / name)
        return out

    paths = await asyncio.to_thread(_walk)
    for p in paths:
        await _ingest_one_write(
            cache_dir=cache_dir,
            cache_path=p,
            session_id=session_id,
            memory_store_id=memory_store_id,
            writer=writer,
        )


async def watch_memory_store(
    session_id: str,
    memory_store_id: uuid.UUID,
    store_name: str,
    *,
    cache_dir: Path | None = None,
    step_ms: int = DEFAULT_STEP_MS,
    debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Asyncio task entry — one per (session, store) for rw stores only.

    Spawned by ``routes/sessions.py`` at session create; cancelled in
    ``terminate_session``. The awatch loop unwinds cleanly on
    ``asyncio.CancelledError``.
    """
    cache = cache_dir or Path(session_memory_cache_dir(session_id, store_name))
    writer = await get_memory_writer()

    await _boot_scan(
        cache_dir=cache,
        session_id=session_id,
        memory_store_id=memory_store_id,
        writer=writer,
    )
    logger.info(
        "memory watcher boot scan complete: session=%s store=%s dir=%s",
        session_id, store_name, cache,
    )

    try:
        async for changes in awatch(
            str(cache),
            step=step_ms,
            debounce=debounce_ms,
            stop_event=stop_event,
            recursive=True,
        ):
            for change_type, path_str in changes:
                cache_path = Path(path_str)
                if change_type in (Change.added, Change.modified):
                    if not cache_path.is_file():
                        continue
                    await _ingest_one_write(
                        cache_dir=cache,
                        cache_path=cache_path,
                        session_id=session_id,
                        memory_store_id=memory_store_id,
                        writer=writer,
                    )
                elif change_type == Change.deleted:
                    await _ingest_one_delete(
                        cache_dir=cache,
                        cache_path=cache_path,
                        session_id=session_id,
                        memory_store_id=memory_store_id,
                        writer=writer,
                    )
    except asyncio.CancelledError:
        logger.info(
            "memory watcher cancelled: session=%s store=%s", session_id, store_name,
        )
        raise
