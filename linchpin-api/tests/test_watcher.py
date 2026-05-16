"""Unit tests for the deliverables watcher (v0.2.0 PR5).

Per eng-review D4/D11/D12, the watcher needs to:
- Ingest files via boot scan (covers api-restart-mid-deliverable),
- Drop oversized files AND unlink the host bytes (no disk leak),
- Drop on aggregate-cap exceeded,
- Be idempotent on sha256 (boot scan over the same dir twice = same state),
- Survive a watcher restart: write → kill → write → restart → assert exactly
  one row per file (D12 hard-stop).

The live ``watchfiles.awatch`` loop isn't unit-tested here — it depends on OS
event timing and would flake. Coverage of the in-process ingest path
(_ingest_one + _boot_scan) is the load-bearing thing; the awatch wrapper
just feeds those.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import watcher as watcher_mod
from app.files import FileTooLargeError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


class _FakeFileStore:
    """Deterministic store stand-in for watcher unit tests.

    Tracks every ``write_from_path`` call so tests can assert what got ingested
    without spinning up the real LocalFileStore (which mkdirs into LINCHPIN_FILES_ROOT).
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.written: list[tuple[str, str, int]] = []  # (storage_path, sha256, size)
        self.raise_on_next: Exception | None = None

    async def write_from_path(self, source_path: str, *, max_bytes: int):
        if self.raise_on_next is not None:
            exc = self.raise_on_next
            self.raise_on_next = None
            raise exc
        size = os.path.getsize(source_path)
        if size > max_bytes:
            raise FileTooLargeError(max_bytes)
        # Hash + return a stable storage_path keyed off content.
        h = watcher_mod._sha256_of_file(source_path)
        storage_path = f"{h[:2]}/{h[2:4]}/{h}"
        self.written.append((storage_path, h, size))
        return (storage_path, h, size)

    def absolute_path(self, storage_path: str) -> str:
        return str(self.root / storage_path)


@pytest.fixture()
def fake_store(tmp_path):
    """A FakeFileStore patched into get_file_store."""
    store = _FakeFileStore(tmp_path / "_filestore_root")
    with patch("app.watcher.get_file_store", return_value=store):
        yield store


@pytest.fixture(autouse=True)
def _mock_db():
    """No real DB — watcher's _aggregate_used_bytes / _already_ingested /
    INSERT all run via mocked app.db helpers."""
    with (
        patch("app.watcher.fetch_one", new_callable=AsyncMock) as mock_fetch_one,
        patch("app.watcher.execute", new_callable=AsyncMock) as mock_execute,
        patch("app.watcher.append_event", new_callable=AsyncMock) as mock_append_event,
    ):
        # Default: empty session, no prior dedup hits.
        mock_fetch_one.return_value = None
        yield {
            "fetch_one": mock_fetch_one,
            "execute": mock_execute,
            "append_event": mock_append_event,
        }


@pytest.fixture()
def small_caps(monkeypatch):
    """Tighten caps so tests don't have to write large files."""
    monkeypatch.setenv("LINCHPIN_FILES_MAX_BYTES", "1024")  # 1 KiB per file
    monkeypatch.setenv("LINCHPIN_DELIVERABLES_PER_SESSION_CAP_BYTES", "4096")  # 4 KiB total


# ---------------------------------------------------------------------------
# Boot scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_scan_ingests_existing_files(tmp_path, fake_store, _mock_db, small_caps):
    """D4 — files written before the watcher starts are picked up on scan."""
    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    _write(outputs / "a.txt", b"alpha")
    _write(outputs / "b.txt", b"beta")

    # _aggregate_used_bytes returns 0; _already_ingested returns False; the
    # INSERT-with-RETURNING-id call returns a row (i.e. did insert, did not
    # lose the dedup race).
    _mock_db["fetch_one"].side_effect = [
        {"total": 0},                 # _aggregate_used_bytes
        None,                         # _already_ingested(a) → False
        {"id": uuid.uuid4()},         # INSERT for a → inserted
        None,                         # _already_ingested(b) → False
        {"id": uuid.uuid4()},         # INSERT for b → inserted
    ]

    used = await watcher_mod._boot_scan("11111111-1111-1111-1111-111111111111", outputs, store=fake_store)
    assert used == len(b"alpha") + len(b"beta")
    assert {sp for sp, _, _ in fake_store.written} == {
        f"{watcher_mod._sha256_of_file(str(outputs / 'a.txt'))[:2]}/"
        f"{watcher_mod._sha256_of_file(str(outputs / 'a.txt'))[2:4]}/"
        f"{watcher_mod._sha256_of_file(str(outputs / 'a.txt'))}",
        f"{watcher_mod._sha256_of_file(str(outputs / 'b.txt'))[:2]}/"
        f"{watcher_mod._sha256_of_file(str(outputs / 'b.txt'))[2:4]}/"
        f"{watcher_mod._sha256_of_file(str(outputs / 'b.txt'))}",
    }
    # Two agent.deliverable events.
    assert _mock_db["append_event"].await_count == 2
    event_types = {call.args[1] for call in _mock_db["append_event"].await_args_list}
    assert event_types == {"agent.deliverable"}


@pytest.mark.asyncio
async def test_boot_scan_idempotent_via_sha256(tmp_path, fake_store, _mock_db, small_caps):
    """Running boot scan twice over the same dir doesn't double-register."""
    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    _write(outputs / "a.txt", b"alpha")

    # First scan: not ingested → register
    _mock_db["fetch_one"].side_effect = [
        {"total": 0},               # aggregate
        None,                       # already_ingested(a) — False
        {"id": uuid.uuid4()},       # INSERT-RETURNING id → row inserted
    ]
    await watcher_mod._boot_scan("11111111-1111-1111-1111-111111111110", outputs, store=fake_store)
    assert len(fake_store.written) == 1

    # Second scan: already_ingested(a) returns a row → no-op
    _mock_db["fetch_one"].side_effect = [
        {"total": 5},  # aggregate
        {"already": True},  # already_ingested(a) — True, dedupe hit
    ]
    await watcher_mod._boot_scan("11111111-1111-1111-1111-111111111110", outputs, store=fake_store)
    assert len(fake_store.written) == 1, "second scan must not re-write"


# ---------------------------------------------------------------------------
# Cap enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_file_cap_drops_and_unlinks(tmp_path, fake_store, _mock_db, small_caps):
    """D4 — file over per-file cap is dropped, host bytes unlinked, event emitted."""
    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    big = _write(outputs / "huge.bin", b"X" * 2048)  # > 1024 per_file_cap

    # _aggregate_used_bytes returns 0 (boot scan path won't be the dropper —
    # ingest_one runs the cap check itself before calling store.write_from_path).
    _mock_db["fetch_one"].return_value = {"total": 0}

    added = await watcher_mod._ingest_one(
        "22222222-2222-2222-2222-222222222220", big,
        store=fake_store,
        per_file_cap=1024,
        aggregate_cap=10_000,
        used_so_far=0,
    )
    assert added == 0
    # FileStore never called.
    assert fake_store.written == []
    # Host bytes unlinked.
    assert not os.path.exists(big)
    # agent.deliverable_dropped event emitted, NOT agent.deliverable.
    _mock_db["append_event"].assert_awaited_once()
    args = _mock_db["append_event"].await_args_list[0].args
    assert args[1] == "agent.deliverable_dropped"
    assert args[2]["reason"] == "per_file_cap_exceeded"


@pytest.mark.asyncio
async def test_aggregate_cap_drops_and_unlinks(tmp_path, fake_store, _mock_db, small_caps):
    """D4 — file that would push session over aggregate cap is dropped."""
    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    f = _write(outputs / "ok.txt", b"OK" * 256)  # 512 bytes — under per-file cap
    _mock_db["fetch_one"].return_value = {"total": 0}

    added = await watcher_mod._ingest_one(
        "33333333-3333-3333-3333-333333333330", f,
        store=fake_store,
        per_file_cap=1024,
        aggregate_cap=400,           # already-used would exceed cap with this write
        used_so_far=100,             # 100 + 512 > 400
    )
    assert added == 0
    assert fake_store.written == []
    assert not os.path.exists(f)
    args = _mock_db["append_event"].await_args_list[0].args
    assert args[1] == "agent.deliverable_dropped"
    assert args[2]["reason"] == "aggregate_cap_exceeded"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_emits_deliverable_event_and_inserts(tmp_path, fake_store, _mock_db, small_caps):
    """Happy path — file within caps gets registered + event fires."""
    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    f = _write(outputs / "report.pdf", b"%PDF-1.4...")

    # _already_ingested returns None (not ingested), then INSERT-RETURNING
    # returns a row id (the dedup race winner is us).
    _mock_db["fetch_one"].side_effect = [
        None,                    # _already_ingested → False
        {"id": uuid.uuid4()},    # INSERT RETURNING id → row inserted
    ]

    added = await watcher_mod._ingest_one(
        "44444444-4444-4444-4444-444444444440", f,
        store=fake_store,
        per_file_cap=10_000,
        aggregate_cap=100_000,
        used_so_far=0,
    )
    assert added == len(b"%PDF-1.4...")
    assert len(fake_store.written) == 1
    # File NOT unlinked on success — the bind owns it; the watcher's job is
    # only to mirror into the Files API.
    assert os.path.exists(f)
    # Event payload carries filename, size, sha256, content_type.
    _mock_db["append_event"].assert_awaited_once()
    args = _mock_db["append_event"].await_args_list[0].args
    assert args[1] == "agent.deliverable"
    payload = args[2]
    assert payload["filename"] == "report.pdf"
    assert payload["size_bytes"] == len(b"%PDF-1.4...")
    assert payload["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_insert_conflict_skips_event(tmp_path, fake_store, _mock_db, small_caps):
    """If the partial unique index (files_session_sha_uidx) fires — i.e. a
    concurrent ingest already wrote this content for the session — the
    INSERT RETURNING id yields no row, and we must NOT emit a duplicate
    agent.deliverable event."""
    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    f = _write(outputs / "race.txt", b"raced")

    # _already_ingested returns None (we passed the application-level check),
    # but the INSERT lost the race to a concurrent writer → RETURNING is
    # empty → fetch_one returns None.
    _mock_db["fetch_one"].side_effect = [
        None,    # _already_ingested → False
        None,    # INSERT RETURNING id → no row (dedup conflict)
    ]

    added = await watcher_mod._ingest_one(
        "44444444-4444-4444-4444-444444444441", f,
        store=fake_store,
        per_file_cap=10_000,
        aggregate_cap=100_000,
        used_so_far=0,
    )
    # Lost the race — don't count it toward the aggregate.
    assert added == 0
    # The agent.deliverable event was already emitted by the race winner;
    # we MUST NOT emit it again.
    _mock_db["append_event"].assert_not_awaited()


@pytest.mark.asyncio
async def test_already_ingested_short_circuits(tmp_path, fake_store, _mock_db, small_caps):
    """Dedupe — if sha256 already registered, no INSERT, no event."""
    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    f = _write(outputs / "dup.txt", b"dup")
    # _already_ingested returns a row (truthy)
    _mock_db["fetch_one"].return_value = {"already": True}

    added = await watcher_mod._ingest_one(
        "55555555-5555-5555-5555-555555555550", f,
        store=fake_store,
        per_file_cap=10_000,
        aggregate_cap=100_000,
        used_so_far=0,
    )
    assert added == 0
    assert fake_store.written == []
    _mock_db["append_event"].assert_not_awaited()


# ---------------------------------------------------------------------------
# D12 — deploy-survives-watcher-restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_recovery_ingests_exactly_once(tmp_path, fake_store, _mock_db, small_caps):
    """D12 hard-stop — write 3 → kill watcher → write 2 → restart → all 5 register
    exactly once.

    Modeled as two ``_boot_scan`` calls (the watcher's restart path runs the
    scan first). After the first scan, the dedup-on-sha256 path makes the
    second scan idempotent for the original 3 files and registers the new 2.
    """
    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    _write(outputs / "1.txt", b"one")
    _write(outputs / "2.txt", b"two")
    _write(outputs / "3.txt", b"three")

    # Scan #1: aggregate 0 → 3 ingests. Each ingest now does TWO fetch_one
    # calls: _already_ingested (None=False), then INSERT RETURNING id (row).
    _mock_db["fetch_one"].side_effect = [
        {"total": 0},               # aggregate
        None, {"id": uuid.uuid4()}, # 1.txt: already=False, INSERT=row
        None, {"id": uuid.uuid4()}, # 2.txt
        None, {"id": uuid.uuid4()}, # 3.txt
    ]
    await watcher_mod._boot_scan("77777777-7777-7777-7777-777777777770", outputs, store=fake_store)
    assert len(fake_store.written) == 3

    # Watcher dies. Two more files written during outage.
    _write(outputs / "4.txt", b"four")
    _write(outputs / "5.txt", b"five")

    # Scan #2 (restart): aggregate seeded from DB to the bytes registered so far;
    # already_ingested returns True for 1/2/3 (short-circuit, no INSERT call),
    # False for 4/5 (followed by an INSERT-RETURNING row).
    seen_bytes = sum(s for _, _, s in fake_store.written)
    _mock_db["fetch_one"].side_effect = [
        {"total": seen_bytes},      # aggregate
        {"already": True},          # 1.txt already ingested
        {"already": True},          # 2.txt already ingested
        {"already": True},          # 3.txt already ingested
        None, {"id": uuid.uuid4()}, # 4.txt new
        None, {"id": uuid.uuid4()}, # 5.txt new
    ]
    await watcher_mod._boot_scan("77777777-7777-7777-7777-777777777770", outputs, store=fake_store)
    assert len(fake_store.written) == 5, "all 5 registered exactly once across restart"
    # Total deliverable events fired: 3 from scan #1 + 2 from scan #2.
    assert _mock_db["append_event"].await_count == 5
    assert all(
        call.args[1] == "agent.deliverable"
        for call in _mock_db["append_event"].await_args_list
    )


# ---------------------------------------------------------------------------
# D11 — synthetic workload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_workload_100_files(tmp_path, fake_store, _mock_db):
    """D11 — write 100 files of varying sizes; all register in <2s.

    Uses the boot scan path (sequential ingest) which is the worst case for
    throughput. The live awatch loop fans events out as they arrive so the
    real-world latency is lower, but if scan-of-100 is fast, awatch-of-100
    is faster.
    """
    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    sizes = [(1 << (i % 10)) for i in range(100)]  # 1B, 2B, 4B, ..., 512B cycling
    for i, size in enumerate(sizes):
        _write(outputs / f"file-{i:03d}.bin", b"X" * size)

    # All new — no dedup hits. Each ingest does TWO fetch_one calls:
    # _already_ingested → None, then INSERT RETURNING id → row.
    _insert_pairs: list = []
    for _ in range(100):
        _insert_pairs.append(None)               # _already_ingested → False
        _insert_pairs.append({"id": uuid.uuid4()})  # INSERT RETURNING id
    _mock_db["fetch_one"].side_effect = [
        {"total": 0},
        *_insert_pairs,
    ]

    started = time.monotonic()
    await watcher_mod._boot_scan("88888888-8888-8888-8888-888888888880", outputs, store=fake_store)
    elapsed = time.monotonic() - started

    assert len(fake_store.written) == 100
    assert elapsed < 2.0, f"100-file boot scan took {elapsed:.2f}s (target <2s)"


# ---------------------------------------------------------------------------
# ensure_session_outputs_dir
# ---------------------------------------------------------------------------


def test_ensure_session_outputs_dir_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("LINCHPIN_SESSION_OUTPUTS_ROOT", str(tmp_path / "outputs"))
    p1 = watcher_mod.ensure_session_outputs_dir("66666666-6666-6666-6666-666666666660")
    assert p1.exists() and p1.is_dir()
    p2 = watcher_mod.ensure_session_outputs_dir("66666666-6666-6666-6666-666666666660")
    assert p1 == p2 and p2.exists()


# ---------------------------------------------------------------------------
# Live awatch loop — smoke test under stop_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_loop_stop_event_exits_cleanly(tmp_path, fake_store, _mock_db, small_caps, monkeypatch):
    """The asyncio task exits cleanly when stop_event is set (the test path
    for graceful shutdown — production uses task.cancel() instead)."""
    monkeypatch.setenv("LINCHPIN_SESSION_OUTPUTS_ROOT", str(tmp_path / "outputs"))
    outputs = tmp_path / "outputs" / "99999999-9999-9999-9999-999999999990"
    outputs.mkdir(parents=True)
    _mock_db["fetch_one"].return_value = {"total": 0}

    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.1)
        stop.set()

    asyncio.create_task(stop_soon())
    await asyncio.wait_for(
        watcher_mod.watch_session_deliverables(
            "99999999-9999-9999-9999-999999999990",
            outputs_dir=outputs,
            step_ms=10,
            debounce_ms=20,
            stop_event=stop,
        ),
        timeout=3.0,
    )


# ---------------------------------------------------------------------------
# Symlink rejection — host-file exfiltration guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_symlink_in_outputs_is_rejected(tmp_path, fake_store, _mock_db, small_caps):
    """Regression: an agent that plants a symlink in the outputs bind cannot
    use it to make the watcher hash/copy an arbitrary host file.

    Without the lstat + O_NOFOLLOW guards, `_sha256_of_file` would follow the
    symlink and `LocalFileStore.write_from_path` would copy the target into
    the FileStore — turning the watcher into a host-file read primitive for
    anything the api process can open.
    """
    secret = tmp_path / "host-only" / "passwd"
    secret.parent.mkdir()
    secret.write_bytes(b"root:x:0:0:host secret\n")

    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    evil = outputs / "evil.txt"
    os.symlink(secret, evil)

    _mock_db["fetch_one"].return_value = {"total": 0}

    added = await watcher_mod._ingest_one(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", str(evil),
        store=fake_store,
        per_file_cap=10_000,
        aggregate_cap=100_000,
        used_so_far=0,
    )
    assert added == 0
    # Nothing ingested.
    assert fake_store.written == []
    # Symlink unlinked, but secret target untouched.
    assert not os.path.lexists(evil), "symlink must be removed"
    assert secret.exists(), "symlink target must NOT be touched"
    assert secret.read_bytes() == b"root:x:0:0:host secret\n"
    # agent.deliverable_dropped event with reason=symlink_rejected.
    _mock_db["append_event"].assert_awaited_once()
    args = _mock_db["append_event"].await_args_list[0].args
    assert args[1] == "agent.deliverable_dropped"
    assert args[2]["reason"] == "symlink_rejected"


@pytest.mark.asyncio
async def test_fifo_in_outputs_is_rejected(tmp_path, fake_store, _mock_db, small_caps):
    """Non-regular files (fifos, sockets, devices) must also be rejected so
    the watcher can't be blocked reading from a named pipe."""
    outputs = tmp_path / "session-outputs"
    outputs.mkdir()
    fifo_path = outputs / "pipe"
    try:
        os.mkfifo(fifo_path)
    except (AttributeError, OSError):
        pytest.skip("platform does not support mkfifo")

    _mock_db["fetch_one"].return_value = {"total": 0}

    added = await watcher_mod._ingest_one(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", str(fifo_path),
        store=fake_store,
        per_file_cap=10_000,
        aggregate_cap=100_000,
        used_so_far=0,
    )
    assert added == 0
    assert fake_store.written == []
    _mock_db["append_event"].assert_awaited_once()
    args = _mock_db["append_event"].await_args_list[0].args
    assert args[1] == "agent.deliverable_dropped"
    assert args[2]["reason"] == "non_regular_file"
