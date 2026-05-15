"""FileStore — content-addressable storage for the Files API (v0.2.0 PR1).

The ``files`` table is the source of truth for metadata; the FileStore owns
the bytes. Files are persisted under a content-addressable layout
(``<root>/sha256[:2]/sha256[2:4]/sha256``), which lets us deduplicate
identical uploads transparently while still letting callers identify a row
by its UUID (the ``storage_path`` column points at the on-disk location).

Only the local-fs backend ships in v0.2.0. The ``FileStore`` ABC exists so
that the S3 backend slated for a later minor can drop in without touching
route code.
"""

from __future__ import annotations

import abc
import hashlib
import logging
import os
import stat
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

logger = logging.getLogger("linchpin-api.files")

DEFAULT_FILES_ROOT = "/var/lib/linchpin/files"
DEFAULT_MAX_BYTES = 500 * 1024 * 1024  # 500 MB
DEFAULT_PER_SESSION_CAP_BYTES = 1024 * 1024 * 1024  # 1 GB — PR5, spec line 320

# Stream chunk size for upload / download. 1 MiB balances memory pressure
# against syscall overhead on typical SSDs.
CHUNK_SIZE = 1024 * 1024


def get_max_bytes() -> int:
    """Per-file upload + deliverable cap from ``LINCHPIN_FILES_MAX_BYTES``."""
    raw = os.environ.get("LINCHPIN_FILES_MAX_BYTES")
    if raw is None:
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"LINCHPIN_FILES_MAX_BYTES must be an integer, got: {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"LINCHPIN_FILES_MAX_BYTES must be positive, got: {value}")
    return value


def get_per_session_cap_bytes() -> int:
    """Aggregate deliverable cap per session from
    ``LINCHPIN_DELIVERABLES_PER_SESSION_CAP_BYTES`` (PR5)."""
    raw = os.environ.get("LINCHPIN_DELIVERABLES_PER_SESSION_CAP_BYTES")
    if raw is None:
        return DEFAULT_PER_SESSION_CAP_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"LINCHPIN_DELIVERABLES_PER_SESSION_CAP_BYTES must be an integer, got: {raw!r}"
        ) from exc
    if value <= 0:
        raise RuntimeError(
            f"LINCHPIN_DELIVERABLES_PER_SESSION_CAP_BYTES must be positive, got: {value}"
        )
    return value


def get_session_outputs_root() -> str:
    """Host-side root for per-session writable output binds (PR5).

    The deliverables watcher (in-process per session per D3) watches
    ``<root>/<session_id>/`` and the container sees that path bind-mounted
    at ``/mnt/session/outputs/`` (writable). Defaults to
    ``LINCHPIN_FILES_ROOT/_session_outputs`` so a single permission check
    on the FileStore root covers it.
    """
    explicit = os.environ.get("LINCHPIN_SESSION_OUTPUTS_ROOT")
    if explicit:
        return explicit
    return os.path.join(
        os.environ.get("LINCHPIN_FILES_ROOT") or DEFAULT_FILES_ROOT,
        "_session_outputs",
    )


class FileTooLargeError(Exception):
    """Raised when an upload exceeds ``LINCHPIN_FILES_MAX_BYTES``."""

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"file exceeds {limit_bytes} byte cap")
        self.limit_bytes = limit_bytes


class FileStore(abc.ABC):
    """Abstract content store. Implementations: ``LocalFileStore``; later ``S3FileStore``."""

    @abc.abstractmethod
    async def write(self, chunks: AsyncIterator[bytes], *, max_bytes: int) -> tuple[str, str, int]:
        """Persist ``chunks`` and return ``(storage_path, sha256, size_bytes)``.

        Raises ``FileTooLargeError`` if the cumulative size exceeds ``max_bytes``;
        partial writes are cleaned up before raising.
        """

    @abc.abstractmethod
    def read(self, storage_path: str) -> Iterable[bytes]:
        """Yield chunks of the file at ``storage_path``.

        Synchronous iterator: FastAPI's StreamingResponse handles either kind,
        and reading from disk doesn't benefit from async.
        """

    @abc.abstractmethod
    async def delete(self, storage_path: str) -> None:
        """Remove the bytes at ``storage_path``. Idempotent."""

    @abc.abstractmethod
    async def write_from_path(
        self, source_path: str, *, max_bytes: int
    ) -> tuple[str, str, int]:
        """Ingest an existing on-disk file at ``source_path`` into the store.

        Returns ``(storage_path, sha256, size_bytes)`` like ``write()``. Used
        by the deliverables watcher (PR5) which sees files the agent already
        wrote to the session-outputs bind. Raises ``FileTooLargeError`` when
        the source exceeds ``max_bytes``; the source file is left in place
        either way (the caller decides whether to ``os.unlink`` on drop).
        """

    @abc.abstractmethod
    def absolute_path(self, storage_path: str) -> str:
        """Return the absolute host filesystem path for ``storage_path``.

        Used by the sandbox to construct bind-mount sources. Backends that
        don't expose a local path (future ``S3FileStore``) should raise
        ``NotImplementedError`` here and rely on a fuse layer that mirrors
        objects under a local prefix; the v0.2 sandbox does not handle
        non-local file stores.
        """


class LocalFileStore(FileStore):
    """Local-filesystem backend rooted at ``LINCHPIN_FILES_ROOT``."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root or os.environ.get("LINCHPIN_FILES_ROOT") or DEFAULT_FILES_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256[2:4] / sha256

    async def write(self, chunks: AsyncIterator[bytes], *, max_bytes: int) -> tuple[str, str, int]:
        # Stream to a temp file under the root, then atomically rename to the
        # content-addressed final path once the hash is known.
        tmp_dir = self.root / "_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"upload-{os.getpid()}-{os.urandom(8).hex()}"

        hasher = hashlib.sha256()
        size = 0
        try:
            with tmp_path.open("wb") as out:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLargeError(max_bytes)
                    hasher.update(chunk)
                    out.write(chunk)
            digest = hasher.hexdigest()
            final = self._path_for(digest)
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                # Content already stored — drop the temp copy.
                tmp_path.unlink(missing_ok=True)
            else:
                os.replace(tmp_path, final)
            return (str(final.relative_to(self.root)), digest, size)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def read(self, storage_path: str) -> Iterable[bytes]:
        full = self.root / storage_path
        if not full.is_file():
            raise FileNotFoundError(storage_path)
        with full.open("rb") as fh:
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    return
                yield chunk

    async def delete(self, storage_path: str) -> None:
        full = self.root / storage_path
        try:
            full.unlink()
        except FileNotFoundError:
            return

    async def write_from_path(
        self, source_path: str, *, max_bytes: int
    ) -> tuple[str, str, int]:
        """Hash + move ``source_path`` into the content-addressed layout.

        Reuses the same sha256-bucketed layout as ``write()``. Opens the
        source with ``O_NOFOLLOW`` so a symlink planted in an agent-writable
        bind mount (PR5 deliverables) cannot make us read or copy an
        arbitrary host file — ``OSError(ELOOP)`` propagates and the watcher
        drops it. ``fstat``-driven size + regular-file check closes the
        TOCTOU window between the watcher's ``lstat`` and our open.
        """
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(source_path, flags)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise IsADirectoryError(
                    f"refusing to ingest non-regular file: {source_path}"
                )
            size = st.st_size
            if size > max_bytes:
                raise FileTooLargeError(max_bytes)
            hasher = hashlib.sha256()
            with os.fdopen(fd, "rb", closefd=False) as fh:
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            final = self._path_for(digest)
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                # Content already stored — caller can drop the source.
                return (str(final.relative_to(self.root)), digest, size)
            # Re-read from the validated fd into the destination so we never
            # re-open the (potentially attacker-controlled) source path.
            os.lseek(fd, 0, os.SEEK_SET)
            with open(final, "wb") as out:
                with os.fdopen(fd, "rb", closefd=False) as fh:
                    while True:
                        chunk = fh.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
            return (str(final.relative_to(self.root)), digest, size)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def absolute_path(self, storage_path: str) -> str:
        return str(self.root / storage_path)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store: FileStore | None = None


def get_file_store() -> FileStore:
    """Return the process-wide FileStore, constructing the local backend if needed."""
    global _store
    if _store is None:
        backend = os.environ.get("LINCHPIN_FILESTORE_BACKEND", "local")
        if backend != "local":
            raise RuntimeError(
                f"LINCHPIN_FILESTORE_BACKEND={backend!r} is not supported in v0.2.0 (use 'local')"
            )
        _store = LocalFileStore()
    return _store


def reset_file_store() -> None:
    """Drop the singleton — used by tests that need a fresh root."""
    global _store
    _store = None
