"""Tests for the per-(session, store) memory writeback watcher (v0.3.0 PR4).

The watcher mirrors agent-side writes in the bind-mounted cache dir back
into the canonical FileStore + DB. These tests pin its sha-based
idempotency, over-cap rollback, symlink rejection, and delete-tombstone
behavior — the four properties PR4's design depends on.

``awatch`` itself isn't covered here (it's the upstream library);
``test_memory_watcher_smoke`` exercises the watcher entrypoint with a
stop_event so the loop wires up cleanly.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory_watcher import (
    _boot_scan,
    _container_path_to_memory_path,
    _ingest_one_delete,
    _ingest_one_write,
    _rollback_over_cap,
    watch_memory_store,
)


# ---------------------------------------------------------------------------
# Path translation
# ---------------------------------------------------------------------------


class TestContainerPathTranslation:
    def test_relative_gets_leading_slash(self):
        assert (
            _container_path_to_memory_path("preferences/formatting.md")
            == "/preferences/formatting.md"
        )

    def test_already_absolute_passes_through(self):
        assert _container_path_to_memory_path("/foo/bar") == "/foo/bar"

    def test_single_file(self):
        assert _container_path_to_memory_path("notes.txt") == "/notes.txt"


# ---------------------------------------------------------------------------
# _ingest_one_write
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def writer_mock() -> MagicMock:
    """MagicMock standing in for MemoryWriter."""
    m = MagicMock()
    m.write = AsyncMock()
    m.delete = AsyncMock()
    m.file_store = MagicMock(return_value=MagicMock())
    return m


@pytest.mark.asyncio
async def test_ingest_write_happy_path(cache_dir, writer_mock):
    """A new file under the cache dir is routed through the writer and
    a memory.write event is emitted."""
    cache_dir.mkdir(parents=True)
    target = cache_dir / "preferences" / "tone.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"be terse")

    result = MagicMock()
    result.memory.id = "mem-1"
    result.seq = 1
    result.action = "create"
    writer_mock.write.return_value = result

    store_id = uuid.uuid4()
    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory_watcher.append_event", new_callable=AsyncMock) as append_event,
    ):
        fetch_one.return_value = None  # no prior head — fresh write
        await _ingest_one_write(
            cache_dir=cache_dir,
            cache_path=target,
            session_id="sess-1",
            memory_store_id=store_id,
            writer=writer_mock,
        )

    writer_mock.write.assert_awaited_once()
    call = writer_mock.write.await_args
    assert call.kwargs["path"] == "/preferences/tone.md"
    assert call.kwargs["content"] == b"be terse"
    assert call.kwargs["author"] == "session:sess-1"

    append_event.assert_awaited_once()
    args, _ = append_event.await_args
    assert args[0] == "sess-1"
    assert args[1] == "memory.write"


@pytest.mark.asyncio
async def test_ingest_write_idempotent_when_sha_matches_head(
    cache_dir, writer_mock,
):
    """If the head sha already matches the file bytes, the watcher
    skips re-writing — boot scan double-fire safety."""
    cache_dir.mkdir(parents=True)
    target = cache_dir / "a.txt"
    target.write_bytes(b"hello")

    head_sha = hashlib.sha256(b"hello").hexdigest()
    store_id = uuid.uuid4()
    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory_watcher.append_event", new_callable=AsyncMock),
    ):
        fetch_one.return_value = {"content_sha256": head_sha}
        await _ingest_one_write(
            cache_dir=cache_dir,
            cache_path=target,
            session_id="sess-1",
            memory_store_id=store_id,
            writer=writer_mock,
        )

    writer_mock.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_write_over_cap_rejected_and_rolled_back(
    cache_dir, writer_mock,
):
    """A write that exceeds the 100KB per-memory cap is rejected and
    the cache file is unlinked (no prior head to restore to)."""
    cache_dir.mkdir(parents=True)
    target = cache_dir / "huge.bin"
    huge = b"\x00" * (101 * 1024)
    target.write_bytes(huge)

    store_id = uuid.uuid4()
    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory_watcher.append_event", new_callable=AsyncMock) as append_event,
    ):
        fetch_one.return_value = None  # no prior head — rollback unlinks
        await _ingest_one_write(
            cache_dir=cache_dir,
            cache_path=target,
            session_id="sess-1",
            memory_store_id=store_id,
            writer=writer_mock,
        )

    writer_mock.write.assert_not_awaited()
    append_event.assert_awaited_once()
    args, _ = append_event.await_args
    assert args[1] == "memory.write_rejected"
    assert args[2]["reason"] == "over_cap"
    assert args[2]["size_bytes"] == len(huge)
    # Rollback unlinks the over-cap file when there's no prior head.
    assert not target.exists()


@pytest.mark.asyncio
async def test_ingest_write_symlink_refused(cache_dir, writer_mock, tmp_path):
    """Symlinks inside the rw bind mount are refused (exfiltration
    guard mirroring the deliverables watcher) — the link is unlinked
    and no write reaches MemoryWriter."""
    cache_dir.mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.write_bytes(b"sensitive")
    link = cache_dir / "leak.txt"
    link.symlink_to(secret)

    store_id = uuid.uuid4()
    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock),
        patch("app.memory_watcher.append_event", new_callable=AsyncMock),
    ):
        await _ingest_one_write(
            cache_dir=cache_dir,
            cache_path=link,
            session_id="sess-1",
            memory_store_id=store_id,
            writer=writer_mock,
        )

    writer_mock.write.assert_not_awaited()
    assert not link.exists()


@pytest.mark.asyncio
async def test_ingest_write_outside_cache_dir_ignored(cache_dir, writer_mock, tmp_path):
    """A path that doesn't sit under the cache dir is silently
    dropped (relative_to ValueError)."""
    cache_dir.mkdir(parents=True)
    rogue = tmp_path / "rogue.txt"
    rogue.write_bytes(b"x")

    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock),
        patch("app.memory_watcher.append_event", new_callable=AsyncMock),
    ):
        await _ingest_one_write(
            cache_dir=cache_dir,
            cache_path=rogue,
            session_id="sess-1",
            memory_store_id=uuid.uuid4(),
            writer=writer_mock,
        )

    writer_mock.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_write_missing_file_silently_returns(cache_dir, writer_mock):
    """A spurious event for a since-deleted file is a no-op, not an
    exception (debounce races)."""
    cache_dir.mkdir(parents=True)
    missing = cache_dir / "gone.txt"

    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock),
        patch("app.memory_watcher.append_event", new_callable=AsyncMock),
    ):
        await _ingest_one_write(
            cache_dir=cache_dir,
            cache_path=missing,
            session_id="sess-1",
            memory_store_id=uuid.uuid4(),
            writer=writer_mock,
        )

    writer_mock.write.assert_not_awaited()


# ---------------------------------------------------------------------------
# _ingest_one_delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_delete_happy_path(cache_dir, writer_mock):
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / "old.md"  # file already deleted on disk

    writer_mock.delete.return_value = "version-id"
    store_id = uuid.uuid4()
    memory_id = uuid.uuid4()
    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory_watcher.append_event", new_callable=AsyncMock) as append_event,
    ):
        fetch_one.side_effect = [
            {"id": memory_id},  # memories lookup for id
            {"seq": 3},          # max seq for event
        ]
        await _ingest_one_delete(
            cache_dir=cache_dir,
            cache_path=cache_path,
            session_id="sess-1",
            memory_store_id=store_id,
            writer=writer_mock,
        )

    writer_mock.delete.assert_awaited_once()
    call = writer_mock.delete.await_args
    assert call.kwargs["path"] == "/old.md"
    assert call.kwargs["author"] == "session:sess-1"
    append_event.assert_awaited_once()
    args, _ = append_event.await_args
    assert args[1] == "memory.write"
    assert args[2]["action"] == "delete"


@pytest.mark.asyncio
async def test_ingest_delete_no_memory_no_event(cache_dir, writer_mock):
    """If the memory wasn't tracked (writer.delete returns None), no
    event is emitted."""
    cache_dir.mkdir(parents=True)
    writer_mock.delete.return_value = None
    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock),
        patch("app.memory_watcher.append_event", new_callable=AsyncMock) as append_event,
    ):
        await _ingest_one_delete(
            cache_dir=cache_dir,
            cache_path=cache_dir / "never-tracked.md",
            session_id="sess-1",
            memory_store_id=uuid.uuid4(),
            writer=writer_mock,
        )
    append_event.assert_not_awaited()


# ---------------------------------------------------------------------------
# _rollback_over_cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_no_prior_unlinks_file(cache_dir):
    cache_dir.mkdir(parents=True)
    target = cache_dir / "doomed.txt"
    target.write_bytes(b"way too big")

    with patch("app.memory_watcher.fetch_one", new_callable=AsyncMock) as fetch_one:
        fetch_one.return_value = None
        await _rollback_over_cap(
            cache_path=target,
            memory_store_id=uuid.uuid4(),
            memory_path="/doomed.txt",
        )

    assert not target.exists()


@pytest.mark.asyncio
async def test_rollback_with_prior_restores_head_bytes(cache_dir, writer_mock):
    """When there's a prior head, the cache file is rewritten with the
    canonical bytes streamed back from the FileStore."""
    cache_dir.mkdir(parents=True)
    target = cache_dir / "restored.txt"
    target.write_bytes(b"giant payload that broke the cap")
    head_sha = hashlib.sha256(b"prior").hexdigest()

    fake_fs = MagicMock()
    fake_fs.read = MagicMock(return_value=iter([b"prior"]))
    writer_mock.file_store.return_value = fake_fs

    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory_watcher.get_memory_writer", new_callable=AsyncMock) as get_writer,
    ):
        fetch_one.return_value = {"content_sha256": head_sha}
        get_writer.return_value = writer_mock
        await _rollback_over_cap(
            cache_path=target,
            memory_store_id=uuid.uuid4(),
            memory_path="/restored.txt",
        )

    assert target.read_bytes() == b"prior"


# ---------------------------------------------------------------------------
# _boot_scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_scan_empty_dir_empty_db_is_noop(cache_dir, writer_mock):
    cache_dir.mkdir(parents=True)
    with patch("app.memory_watcher.fetch_one", new_callable=AsyncMock) as fetch_one:
        fetch_one.return_value = {"c": 0}
        await _boot_scan(
            cache_dir=cache_dir,
            session_id="sess-1",
            memory_store_id=uuid.uuid4(),
            writer=writer_mock,
        )
    writer_mock.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_boot_scan_walks_and_ingests_each_file(cache_dir, writer_mock):
    cache_dir.mkdir(parents=True)
    (cache_dir / "a.txt").write_bytes(b"alpha")
    (cache_dir / "sub").mkdir()
    (cache_dir / "sub" / "b.txt").write_bytes(b"beta")

    result = MagicMock()
    result.memory.id = "mem-1"
    result.seq = 1
    result.action = "create"
    writer_mock.write.return_value = result

    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory_watcher.append_event", new_callable=AsyncMock),
    ):
        # First call is the COUNT(*) in boot_scan; subsequent are the
        # per-file head lookups during ingest. Use side_effect with a
        # default-returning callable.
        responses = [{"c": 0}, None, None]  # count=0 won't short-circuit because dir non-empty
        fetch_one.side_effect = responses
        await _boot_scan(
            cache_dir=cache_dir,
            session_id="sess-1",
            memory_store_id=uuid.uuid4(),
            writer=writer_mock,
        )

    assert writer_mock.write.await_count == 2
    seen_paths = {c.kwargs["path"] for c in writer_mock.write.await_args_list}
    assert seen_paths == {"/a.txt", "/sub/b.txt"}


# ---------------------------------------------------------------------------
# watch_memory_store entrypoint (smoke)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_memory_store_terminates_on_stop_event(cache_dir, writer_mock):
    """Smoke-test the entry point: with a pre-set stop event and an
    empty cache dir, the loop exits cleanly without invoking writer."""
    cache_dir.mkdir(parents=True)
    stop = asyncio.Event()
    stop.set()

    with (
        patch("app.memory_watcher.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory_watcher.get_memory_writer", new_callable=AsyncMock) as get_writer,
    ):
        fetch_one.return_value = {"c": 0}
        get_writer.return_value = writer_mock
        await asyncio.wait_for(
            watch_memory_store(
                "sess-1",
                uuid.uuid4(),
                "preferences",
                cache_dir=cache_dir,
                stop_event=stop,
            ),
            timeout=2.0,
        )

    writer_mock.write.assert_not_awaited()
