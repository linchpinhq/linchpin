"""Tests for the memory version GC pass (v0.3.0 PR5).

The GC pass tombstones ``memory_versions`` rows whose ``expires_at <
now()``, frees their FileStore bytes when no live version dedupes to
the same path, and is idempotent across re-runs. These tests pin all
three properties at the DB-mock boundary so we don't need a real
Postgres + FileStore to assert behavior.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory import (
    cleanup_expired_memory_versions,
    memory_gc_batch_size,
    memory_gc_interval_seconds,
    run_memory_gc,
)


# ---------------------------------------------------------------------------
# Env-driven config helpers
# ---------------------------------------------------------------------------


class TestGcConfig:
    def test_interval_default(self, monkeypatch):
        monkeypatch.delenv("LINCHPIN_MEMORY_GC_INTERVAL_SEC", raising=False)
        assert memory_gc_interval_seconds() == 3600

    def test_interval_override(self, monkeypatch):
        monkeypatch.setenv("LINCHPIN_MEMORY_GC_INTERVAL_SEC", "120")
        assert memory_gc_interval_seconds() == 120

    def test_interval_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("LINCHPIN_MEMORY_GC_INTERVAL_SEC", "not-a-number")
        assert memory_gc_interval_seconds() == 3600

    def test_interval_floor_is_one(self, monkeypatch):
        monkeypatch.setenv("LINCHPIN_MEMORY_GC_INTERVAL_SEC", "0")
        assert memory_gc_interval_seconds() == 1

    def test_batch_size_default(self, monkeypatch):
        monkeypatch.delenv("LINCHPIN_MEMORY_GC_BATCH_SIZE", raising=False)
        assert memory_gc_batch_size() == 5000

    def test_batch_size_override(self, monkeypatch):
        monkeypatch.setenv("LINCHPIN_MEMORY_GC_BATCH_SIZE", "10")
        assert memory_gc_batch_size() == 10


# ---------------------------------------------------------------------------
# run_memory_gc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_memory_gc_no_expired_rows_is_noop():
    """No expired rows → SELECT returns []; no UPDATE/DELETE issued, counts zero."""
    with (
        patch("app.memory.fetch_all", new_callable=AsyncMock) as fetch_all,
        patch("app.memory.execute", new_callable=AsyncMock) as execute,
        patch("app.memory.fetch_one", new_callable=AsyncMock),
        patch("app.memory.LocalFileStore") as MockFs,
    ):
        fetch_all.return_value = []
        result = await run_memory_gc()

    assert result == {"versions_redacted": 0, "bytes_freed": 0}
    execute.assert_not_called()
    MockFs.return_value.delete.assert_not_called()


@pytest.mark.asyncio
async def test_run_memory_gc_tombstones_each_expired_row():
    """Each expired version is UPDATE'd to redacted_at + zero sha +
    empty storage_path. Counts mirror the affected row count + bytes."""
    rows = [
        {"id": uuid.uuid4(), "storage_path": "aa/bb/sha-a", "size_bytes": 100},
        {"id": uuid.uuid4(), "storage_path": "cc/dd/sha-b", "size_bytes": 250},
    ]
    fake_fs = MagicMock()
    fake_fs.delete = AsyncMock()
    with (
        patch("app.memory.fetch_all", new_callable=AsyncMock) as fetch_all,
        patch("app.memory.execute", new_callable=AsyncMock) as execute,
        patch("app.memory.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory.LocalFileStore", return_value=fake_fs),
    ):
        fetch_all.return_value = rows
        fetch_one.return_value = None  # no other live version shares the storage_path
        result = await run_memory_gc()

    assert result == {"versions_redacted": 2, "bytes_freed": 350}
    # Two UPDATE statements, one per row.
    assert execute.await_count == 2
    update_sql, _ts, row_id_first = execute.await_args_list[0].args
    assert "redacted_at" in update_sql
    assert "repeat('0', 64)" in update_sql
    # Bytes unlinked from the FileStore for each storage_path.
    assert fake_fs.delete.await_count == 2
    deleted_paths = {c.args[0] for c in fake_fs.delete.await_args_list}
    assert deleted_paths == {"aa/bb/sha-a", "cc/dd/sha-b"}


@pytest.mark.asyncio
async def test_run_memory_gc_keeps_storage_when_other_live_version_dedupes():
    """If another non-redacted version still points at the same
    storage_path, GC tombstones the row but does NOT unlink the
    FileStore object."""
    storage_path = "ee/ff/shared-sha"
    rows = [{"id": uuid.uuid4(), "storage_path": storage_path, "size_bytes": 42}]
    fake_fs = MagicMock()
    fake_fs.delete = AsyncMock()

    with (
        patch("app.memory.fetch_all", new_callable=AsyncMock) as fetch_all,
        patch("app.memory.execute", new_callable=AsyncMock),
        patch("app.memory.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory.LocalFileStore", return_value=fake_fs),
    ):
        fetch_all.return_value = rows
        fetch_one.return_value = {"x": 1}  # another live version exists
        result = await run_memory_gc()

    assert result["versions_redacted"] == 1
    fake_fs.delete.assert_not_called()


@pytest.mark.asyncio
async def test_run_memory_gc_handles_storage_filenotfound_silently():
    """A missing FileStore object (already cleaned up out of band)
    doesn't fail the pass — the DB tombstone is the source of truth."""
    rows = [{"id": uuid.uuid4(), "storage_path": "gg/hh/missing", "size_bytes": 50}]
    fake_fs = MagicMock()
    fake_fs.delete = AsyncMock(side_effect=FileNotFoundError)

    with (
        patch("app.memory.fetch_all", new_callable=AsyncMock) as fetch_all,
        patch("app.memory.execute", new_callable=AsyncMock),
        patch("app.memory.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory.LocalFileStore", return_value=fake_fs),
    ):
        fetch_all.return_value = rows
        fetch_one.return_value = None
        result = await run_memory_gc()

    assert result == {"versions_redacted": 1, "bytes_freed": 50}


@pytest.mark.asyncio
async def test_run_memory_gc_skips_storage_unlink_when_path_empty():
    """A row that was already half-redacted (storage_path='') doesn't
    trigger any FileStore.delete call."""
    rows = [{"id": uuid.uuid4(), "storage_path": "", "size_bytes": 0}]
    fake_fs = MagicMock()
    fake_fs.delete = AsyncMock()
    with (
        patch("app.memory.fetch_all", new_callable=AsyncMock) as fetch_all,
        patch("app.memory.execute", new_callable=AsyncMock),
        patch("app.memory.fetch_one", new_callable=AsyncMock) as fetch_one,
        patch("app.memory.LocalFileStore", return_value=fake_fs),
    ):
        fetch_all.return_value = rows
        fetch_one.return_value = None
        await run_memory_gc()

    fake_fs.delete.assert_not_called()


@pytest.mark.asyncio
async def test_run_memory_gc_uses_explicit_batch_size_when_passed():
    """Caller-supplied ``batch_size`` overrides the env-driven default."""
    with (
        patch("app.memory.fetch_all", new_callable=AsyncMock) as fetch_all,
        patch("app.memory.execute", new_callable=AsyncMock),
        patch("app.memory.fetch_one", new_callable=AsyncMock),
        patch("app.memory.LocalFileStore"),
    ):
        fetch_all.return_value = []
        await run_memory_gc(batch_size=17)

    fetch_all.assert_awaited_once()
    sql, limit = fetch_all.await_args.args
    assert "LIMIT $1" in sql
    assert limit == 17


# ---------------------------------------------------------------------------
# cleanup_expired_memory_versions (the periodic loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_loop_runs_gc_and_continues_after_pass():
    """One tick of the loop calls ``run_memory_gc`` and continues. We
    cancel after the first call to keep the test bounded."""
    calls = {"n": 0}

    async def _fake_gc(*a, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError()
        return {"versions_redacted": 5, "bytes_freed": 1024}

    with (
        patch("app.memory.run_memory_gc", new=_fake_gc),
        patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock,
    ):
        with pytest.raises(asyncio.CancelledError):
            await cleanup_expired_memory_versions(interval_seconds=1)

    assert calls["n"] == 2
    # Sleep called once per tick.
    assert sleep_mock.await_count == 2


@pytest.mark.asyncio
async def test_cleanup_loop_swallows_transient_exception():
    """A transient error in a GC pass doesn't kill the loop; the next
    tick proceeds normally."""
    calls = {"n": 0}

    async def _flaky_gc(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("postgres hiccup")
        # On the second pass, cancel out cleanly.
        raise asyncio.CancelledError()

    with (
        patch("app.memory.run_memory_gc", new=_flaky_gc),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        with pytest.raises(asyncio.CancelledError):
            await cleanup_expired_memory_versions(interval_seconds=1)

    assert calls["n"] == 2
