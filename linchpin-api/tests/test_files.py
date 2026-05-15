"""Unit tests for the Files API (v0.2.0 PR1).

Covers:
- FileStore (LocalFileStore) — round-trip, sha256 stability, dedup, size cap, delete idempotence.
- POST /v1/files — upload happy path, 413 oversize.
- GET /v1/files — scope filter (with + without scope_id).
- GET /v1/files/{id} — 200 + 404.
- GET /v1/files/{id}/content — 200 stream, 403 not_downloadable, 404 archived.
- DELETE /v1/files/{id} — 204, 404, dedup (bytes preserved if another row references same path).
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import files as files_module
from app.files import FileTooLargeError, LocalFileStore


AUTH = {"Authorization": "Bearer test-secret-key"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file_row(
    *,
    file_id: str | None = None,
    filename: str = "report.pdf",
    content_type: str = "application/pdf",
    size_bytes: int = 1024,
    storage_path: str = "ab/cd/abcd...",
    sha256: str = "abcd" * 16,
    source: str = "upload",
    downloadable: bool = False,
    scope_type: str | None = None,
    scope_id: str | None = None,
    archived_at=None,
):
    """Build an asyncpg Record-like dict for a files row."""
    return {
        "id": uuid.UUID(file_id) if file_id else uuid.uuid4(),
        "filename": filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "storage_path": storage_path,
        "sha256": sha256,
        "source": source,
        "downloadable": downloadable,
        "scope_type": scope_type,
        "scope_id": uuid.UUID(scope_id) if scope_id else None,
        "created_at": datetime(2026, 5, 13, tzinfo=timezone.utc),
        "archived_at": archived_at,
    }


async def _chunks_from_bytes(data: bytes, chunk_size: int = 32) -> AsyncIterator[bytes]:
    """Yield ``data`` in fixed-size async chunks."""
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


# ---------------------------------------------------------------------------
# FileStore unit tests (no FastAPI / no DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_file_store_round_trip(tmp_path):
    store = LocalFileStore(root=tmp_path)
    payload = b"hello linchpin files api"

    storage_path, sha, size = await store.write(_chunks_from_bytes(payload), max_bytes=1_000_000)

    assert size == len(payload)
    # Read it back through the store interface.
    recovered = b"".join(store.read(storage_path))
    assert recovered == payload

    # The sha256 in the return value matches what hashlib computes directly.
    import hashlib
    assert sha == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_local_file_store_deduplicates_identical_content(tmp_path):
    store = LocalFileStore(root=tmp_path)
    payload = b"duplicate bytes" * 100

    p1, sha1, _ = await store.write(_chunks_from_bytes(payload), max_bytes=1_000_000)
    p2, sha2, _ = await store.write(_chunks_from_bytes(payload), max_bytes=1_000_000)

    assert p1 == p2
    assert sha1 == sha2
    # Only one on-disk file — count regular files under root excluding _tmp dir.
    on_disk = [p for p in tmp_path.rglob("*") if p.is_file() and "_tmp" not in p.parts]
    assert len(on_disk) == 1


@pytest.mark.asyncio
async def test_local_file_store_enforces_max_bytes(tmp_path):
    store = LocalFileStore(root=tmp_path)
    payload = b"x" * 1000

    with pytest.raises(FileTooLargeError) as excinfo:
        await store.write(_chunks_from_bytes(payload, chunk_size=128), max_bytes=200)
    assert excinfo.value.limit_bytes == 200

    # No on-disk content survived the failed write.
    on_disk = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert on_disk == []


@pytest.mark.asyncio
async def test_local_file_store_delete_is_idempotent(tmp_path):
    store = LocalFileStore(root=tmp_path)
    storage_path, _, _ = await store.write(_chunks_from_bytes(b"to-delete"), max_bytes=1024)

    await store.delete(storage_path)
    # Second delete of a missing path must not raise.
    await store.delete(storage_path)


def test_get_max_bytes_default_when_unset(monkeypatch):
    monkeypatch.delenv("LINCHPIN_FILES_MAX_BYTES", raising=False)
    assert files_module.get_max_bytes() == files_module.DEFAULT_MAX_BYTES


def test_get_max_bytes_rejects_non_positive(monkeypatch):
    monkeypatch.setenv("LINCHPIN_FILES_MAX_BYTES", "0")
    with pytest.raises(RuntimeError):
        files_module.get_max_bytes()


# ---------------------------------------------------------------------------
# Route tests — FastAPI level, DB + FileStore mocked
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_store():
    """A FileStore stub that returns deterministic write results and replays reads."""
    store = MagicMock()
    store.write = AsyncMock(return_value=("ab/cd/" + "ab" * 32, "ab" * 32, 17))
    store.delete = AsyncMock()
    # ``read`` is sync-yielding per the FileStore contract.
    store.read = MagicMock(return_value=iter([b"hello ", b"world"]))
    return store


# ---- POST /v1/files ----


@patch("app.routes.files.get_file_store")
@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_upload_file_returns_201_with_metadata(mock_fetch, mock_get_store, fake_store, client):
    mock_get_store.return_value = fake_store
    file_id = str(uuid.uuid4())
    mock_fetch.return_value = _make_file_row(
        file_id=file_id,
        filename="hello.txt",
        content_type="text/plain",
        size_bytes=17,
        storage_path="ab/cd/" + "ab" * 32,
        sha256="ab" * 32,
        source="upload",
        downloadable=False,
    )

    resp = client.post(
        "/v1/files",
        files={"file": ("hello.txt", io.BytesIO(b"hello linchpin!"), "text/plain")},
        headers=AUTH,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == file_id
    assert body["filename"] == "hello.txt"
    assert body["content_type"] == "text/plain"
    assert body["size_bytes"] == 17
    assert body["source"] == "upload"
    assert body["downloadable"] is False
    assert body["scope_type"] is None
    assert body["scope_id"] is None
    # FileStore got invoked exactly once with the per-file cap.
    assert fake_store.write.await_count == 1


@patch("app.routes.files.get_file_store")
def test_upload_file_returns_413_when_too_large(mock_get_store, client):
    store = MagicMock()
    store.write = AsyncMock(side_effect=FileTooLargeError(500))
    mock_get_store.return_value = store

    resp = client.post(
        "/v1/files",
        files={"file": ("big.bin", io.BytesIO(b"x" * 600), "application/octet-stream")},
        headers=AUTH,
    )

    assert resp.status_code == 413
    body = resp.json()
    assert body["detail"]["error"] == "file_too_large"
    assert body["detail"]["limit_bytes"] == 500


# ---- GET /v1/files ----


@patch("app.routes.files.fetch_all", new_callable=AsyncMock)
def test_list_files_unscoped_only_returns_unscoped_uploads(mock_fetch_all, client):
    mock_fetch_all.return_value = [_make_file_row(filename="a.txt"), _make_file_row(filename="b.txt")]

    resp = client.get("/v1/files", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    # The query used must filter scope_type IS NULL — assert it via the SQL passed.
    sql_called = mock_fetch_all.await_args.args[0]
    assert "scope_type IS NULL" in sql_called


@patch("app.routes.files.fetch_all", new_callable=AsyncMock)
def test_list_files_scope_filter_is_exact(mock_fetch_all, client):
    scope = str(uuid.uuid4())
    mock_fetch_all.return_value = [
        _make_file_row(filename="report.pdf", scope_type="session", scope_id=scope, downloadable=True, source="deliverable"),
    ]

    resp = client.get(f"/v1/files?scope_id={scope}", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["scope_id"] == scope
    assert body["data"][0]["source"] == "deliverable"
    # Confirm the query filters exactly (no prefix match) and binds the scope UUID.
    sql_called = mock_fetch_all.await_args.args[0]
    bound_scope = mock_fetch_all.await_args.args[1]
    assert "scope_id = $1" in sql_called
    assert "scope_type = 'session'" in sql_called
    assert str(bound_scope) == scope


@patch("app.routes.files.fetch_all", new_callable=AsyncMock)
def test_list_files_pagination_has_more(mock_fetch_all, client):
    # Return limit+1 rows to indicate more available.
    mock_fetch_all.return_value = [_make_file_row(filename=f"f{i}") for i in range(3)]

    resp = client.get("/v1/files?limit=2", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["has_more"] is True


def test_list_files_rejects_malformed_scope_id(client):
    resp = client.get("/v1/files?scope_id=not-a-uuid", headers=AUTH)
    assert resp.status_code == 404


# ---- GET /v1/files/{id} ----


@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_get_file_returns_metadata(mock_fetch, client):
    file_id = str(uuid.uuid4())
    mock_fetch.return_value = _make_file_row(file_id=file_id, filename="x.pdf")

    resp = client.get(f"/v1/files/{file_id}", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["id"] == file_id


@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_get_file_returns_404_when_missing(mock_fetch, client):
    mock_fetch.return_value = None
    resp = client.get(f"/v1/files/{uuid.uuid4()}", headers=AUTH)
    assert resp.status_code == 404


@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_get_file_returns_404_when_archived(mock_fetch, client):
    mock_fetch.return_value = _make_file_row(archived_at=datetime(2026, 5, 13, tzinfo=timezone.utc))
    resp = client.get(f"/v1/files/{uuid.uuid4()}", headers=AUTH)
    assert resp.status_code == 404


# ---- GET /v1/files/{id}/content ----


@patch("app.routes.files.get_file_store")
@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_get_file_content_returns_403_for_upload(mock_fetch, mock_get_store, fake_store, client):
    """Uploads are never downloadable — content stream must 403."""
    mock_get_store.return_value = fake_store
    mock_fetch.return_value = _make_file_row(source="upload", downloadable=False)

    resp = client.get(f"/v1/files/{uuid.uuid4()}/content", headers=AUTH)

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "not_downloadable"
    # Store must NOT have been consulted once the gate rejects.
    fake_store.read.assert_not_called()


@patch("app.routes.files.get_file_store")
@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_get_file_content_streams_bytes_for_deliverable(mock_fetch, mock_get_store, fake_store, client):
    mock_get_store.return_value = fake_store
    mock_fetch.return_value = _make_file_row(
        filename="report.pdf",
        content_type="application/pdf",
        source="deliverable",
        downloadable=True,
        scope_type="session",
        scope_id=str(uuid.uuid4()),
    )

    resp = client.get(f"/v1/files/{uuid.uuid4()}/content", headers=AUTH)

    assert resp.status_code == 200
    assert resp.content == b"hello world"
    assert resp.headers["content-type"].startswith("application/pdf")
    assert 'filename="report.pdf"' in resp.headers["content-disposition"]


@patch("app.routes.files.get_file_store")
@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_get_file_content_returns_404_when_missing(mock_fetch, mock_get_store, fake_store, client):
    mock_get_store.return_value = fake_store
    mock_fetch.return_value = None
    resp = client.get(f"/v1/files/{uuid.uuid4()}/content", headers=AUTH)
    assert resp.status_code == 404


# ---- DELETE /v1/files/{id} ----


@patch("app.routes.files.get_file_store")
@patch("app.routes.files.execute", new_callable=AsyncMock)
@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_delete_file_removes_row_and_bytes_when_unreferenced(
    mock_fetch, mock_execute, mock_get_store, fake_store, client
):
    mock_get_store.return_value = fake_store
    # fetch_one order (PR3): target row → mount-conflict check (None) → dedup check (None).
    target = _make_file_row(storage_path="aa/bb/" + "aa" * 32)
    mock_fetch.side_effect = [target, None, None]

    resp = client.delete(f"/v1/files/{uuid.uuid4()}", headers=AUTH)

    assert resp.status_code == 204
    mock_execute.assert_awaited_once()
    fake_store.delete.assert_awaited_once_with("aa/bb/" + "aa" * 32)


@patch("app.routes.files.get_file_store")
@patch("app.routes.files.execute", new_callable=AsyncMock)
@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_delete_file_preserves_bytes_when_storage_path_still_referenced(
    mock_fetch, mock_execute, mock_get_store, fake_store, client
):
    """If another row points at the same content-addressed bytes, don't drop them."""
    mock_get_store.return_value = fake_store
    target = _make_file_row(storage_path="cc/dd/" + "cc" * 32)
    # fetch_one order (PR3): target → mount check (None) → dedup check (sentinel row).
    mock_fetch.side_effect = [target, None, {"sentinel": True}]

    resp = client.delete(f"/v1/files/{uuid.uuid4()}", headers=AUTH)

    assert resp.status_code == 204
    mock_execute.assert_awaited_once()
    fake_store.delete.assert_not_awaited()


@patch("app.routes.files.execute", new_callable=AsyncMock)
@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_delete_file_returns_409_when_mounted(mock_fetch, mock_execute, client):
    """PR3 — D10: DELETE 409s when the file is actively mounted into a session.
    The error body identifies which session + mount_path so the caller can
    terminate or unmount before retrying."""
    target = _make_file_row(storage_path="ee/ff/" + "ee" * 32)
    mounted_row = {
        "id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "mount_path": "/mnt/data/active.csv",
    }
    # fetch_one order: target row → mount check returns a row → no further calls.
    mock_fetch.side_effect = [target, mounted_row]

    resp = client.delete(f"/v1/files/{uuid.uuid4()}", headers=AUTH)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "file_in_use"
    assert detail["session_id"] == str(mounted_row["session_id"])
    assert detail["mount_path"] == "/mnt/data/active.csv"
    # No DELETE issued — file row + bytes preserved.
    mock_execute.assert_not_awaited()


@patch("app.routes.files.fetch_one", new_callable=AsyncMock)
def test_delete_file_returns_404_when_missing(mock_fetch, client):
    mock_fetch.return_value = None
    resp = client.delete(f"/v1/files/{uuid.uuid4()}", headers=AUTH)
    assert resp.status_code == 404


def test_delete_file_returns_404_for_malformed_id(client):
    resp = client.delete("/v1/files/not-a-uuid", headers=AUTH)
    assert resp.status_code == 404
