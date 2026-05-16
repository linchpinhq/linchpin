"""Tests for memory store + memory CRUD (v0.3.0 PR1).

The HTTP route tests mock asyncpg out at the ``app.routes.memory_stores``
and ``app.routes.memories`` module boundary; the MemoryWriter tests
exercise the validator / precondition matrix directly. Sandbox mount
+ watcher integration tests land in PR4.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory import (
    PreconditionFailed,
    memory_retention_days,
    memory_store_root,
)
from app.models import (
    MEMORY_MAX_BYTES_PER_MEMORY,
    ContentShaPrecondition,
    validate_memory_path,
)


AUTH = {"Authorization": "Bearer test-secret-key"}


# ---------------------------------------------------------------------------
# Path validator
# ---------------------------------------------------------------------------


class TestValidateMemoryPath:
    @pytest.mark.parametrize("good", [
        "/foo",
        "/foo/bar.md",
        "/preferences/formatting.md",
        "/a/very/deep/path/with/many/segments/file.txt",
        "/a",
    ])
    def test_accepts_valid(self, good):
        assert validate_memory_path(good) == good

    @pytest.mark.parametrize("bad", [
        "",                  # empty
        "no-leading-slash",
        "/",                 # bare root
        "/trailing/",        # trailing slash
        "/foo//bar",         # empty segment
        "/foo/../bar",       # parent traversal
        "/foo/\x00/bar",     # NUL byte
        "/" + "x" * 5000,    # too long
    ])
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            validate_memory_path(bad)


# ---------------------------------------------------------------------------
# Memory store CRUD (route layer, mocked DB)
# ---------------------------------------------------------------------------


def _make_store_row(
    *,
    store_id: uuid.UUID | None = None,
    name: str = "test-store",
    description: str | None = None,
    archived_at=None,
):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    return {
        "id": store_id or uuid.uuid4(),
        "name": name,
        "description": description,
        "workspace_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "created_at": now,
        "updated_at": now,
        "archived_at": archived_at,
    }


@patch("app.routes.memory_stores.fetch_one", new_callable=AsyncMock)
def test_create_memory_store_returns_201(mock_fetch, client):
    # First call: uniqueness check (no conflict). Second: INSERT RETURNING *.
    mock_fetch.side_effect = [None, _make_store_row(name="my-mem")]

    resp = client.post(
        "/v1/memory_stores",
        json={"name": "my-mem", "description": "user prefs"},
        headers=AUTH,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "my-mem"
    assert "id" in body


@patch("app.routes.memory_stores.fetch_one", new_callable=AsyncMock)
def test_create_memory_store_409_on_name_conflict(mock_fetch, client):
    mock_fetch.return_value = {"id": uuid.uuid4()}
    resp = client.post(
        "/v1/memory_stores",
        json={"name": "duplicate"},
        headers=AUTH,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "name_conflict"


@pytest.mark.parametrize("bad_name", [
    "Has-Uppercase",
    "name with space",
    "trailing-hyphen-",
    "-leading-hyphen",
    "name_with_underscore",
    "anthropic",  # reserved
    "claude",
    "linchpin",
])
def test_create_memory_store_rejects_bad_name(client, bad_name):
    resp = client.post(
        "/v1/memory_stores",
        json={"name": bad_name},
        headers=AUTH,
    )
    assert resp.status_code == 422


@patch("app.routes.memory_stores.fetch_one", new_callable=AsyncMock)
def test_get_memory_store_returns_200(mock_fetch, client):
    sid = str(uuid.uuid4())
    mock_fetch.return_value = _make_store_row(store_id=uuid.UUID(sid))
    resp = client.get(f"/v1/memory_stores/{sid}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["id"] == sid


@patch("app.routes.memory_stores.fetch_one", new_callable=AsyncMock)
def test_get_memory_store_404(mock_fetch, client):
    mock_fetch.return_value = None
    resp = client.get(f"/v1/memory_stores/{uuid.uuid4()}", headers=AUTH)
    assert resp.status_code == 404


def test_get_memory_store_404_for_malformed_id(client):
    resp = client.get("/v1/memory_stores/not-a-uuid", headers=AUTH)
    assert resp.status_code == 404


@patch("app.routes.memory_stores.fetch_one", new_callable=AsyncMock)
def test_patch_memory_store_updates_description(mock_fetch, client):
    sid = uuid.uuid4()
    existing = _make_store_row(store_id=sid)
    updated = _make_store_row(store_id=sid, description="new desc")
    mock_fetch.side_effect = [existing, updated]
    resp = client.patch(
        f"/v1/memory_stores/{sid}",
        json={"description": "new desc"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == "new desc"


@patch("app.routes.memory_stores.fetch_one", new_callable=AsyncMock)
def test_archive_memory_store(mock_fetch, client):
    sid = uuid.uuid4()
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    existing = _make_store_row(store_id=sid)
    archived = _make_store_row(store_id=sid, archived_at=now)
    mock_fetch.side_effect = [existing, archived]
    resp = client.post(f"/v1/memory_stores/{sid}/archive", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is not None


@patch("app.routes.memory_stores.execute", new_callable=AsyncMock)
@patch("app.routes.memory_stores.fetch_one", new_callable=AsyncMock)
def test_delete_memory_store_204_when_unmounted(mock_fetch, mock_execute, client):
    sid = uuid.uuid4()
    mock_fetch.side_effect = [
        {"id": sid},      # existence check
        None,             # no conflicting session
    ]
    resp = client.delete(f"/v1/memory_stores/{sid}", headers=AUTH)
    assert resp.status_code == 204
    mock_execute.assert_awaited_once()


@patch("app.routes.memory_stores.fetch_one", new_callable=AsyncMock)
def test_delete_memory_store_409_when_mounted(mock_fetch, client):
    sid = uuid.uuid4()
    session_id = uuid.uuid4()
    mock_fetch.side_effect = [
        {"id": sid},
        {"session_id": session_id, "status": "running"},
    ]
    resp = client.delete(f"/v1/memory_stores/{sid}", headers=AUTH)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "memory_store_in_use"
    assert resp.json()["detail"]["blocking_session_id"] == str(session_id)


def test_memory_store_endpoints_require_auth(client):
    sid = uuid.uuid4()
    assert client.get("/v1/memory_stores").status_code == 401
    assert client.post("/v1/memory_stores", json={"name": "x"}).status_code == 401
    assert client.get(f"/v1/memory_stores/{sid}").status_code == 401
    assert client.delete(f"/v1/memory_stores/{sid}").status_code == 401


# ---------------------------------------------------------------------------
# Memory CRUD — input validation only (writer mocked)
# ---------------------------------------------------------------------------


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def test_write_memory_rejects_oversize_payload(client):
    sid = uuid.uuid4()
    payload = {
        "path": "/foo.md",
        "content": _b64(b"x" * (MEMORY_MAX_BYTES_PER_MEMORY + 1)),
    }
    resp = client.post(
        f"/v1/memory_stores/{sid}/memories",
        json=payload,
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_write_memory_rejects_bad_path(client):
    sid = uuid.uuid4()
    payload = {"path": "no-leading-slash", "content": _b64(b"hello")}
    resp = client.post(
        f"/v1/memory_stores/{sid}/memories",
        json=payload,
        headers=AUTH,
    )
    assert resp.status_code == 422


@patch("app.routes.memories.fetch_one", new_callable=AsyncMock)
def test_get_memory_by_path_404_when_store_missing(mock_fetch, client):
    mock_fetch.return_value = None
    sid = uuid.uuid4()
    resp = client.get(
        f"/v1/memory_stores/{sid}/memories?path=/foo.md",
        headers=AUTH,
    )
    assert resp.status_code == 404


def test_list_memory_rejects_bad_path_prefix(client):
    """``path_prefix`` must be an absolute path. The route catches the
    422 before any DB call."""
    sid = uuid.uuid4()
    # Need the store to exist for the validation to be reached.
    with patch("app.routes.memories.fetch_one", new_callable=AsyncMock) as mf:
        mf.return_value = {"id": sid}
        resp = client.get(
            f"/v1/memory_stores/{sid}/memories?path_prefix=no-slash",
            headers=AUTH,
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_path_prefix"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestEnvConfig:
    def test_retention_days_default_30(self, monkeypatch):
        monkeypatch.delenv("LINCHPIN_MEMORY_VERSION_RETENTION_DAYS", raising=False)
        assert memory_retention_days() == 30

    def test_retention_days_capped_at_365(self, monkeypatch):
        monkeypatch.setenv("LINCHPIN_MEMORY_VERSION_RETENTION_DAYS", "9999")
        assert memory_retention_days() == 365

    def test_retention_days_min_1(self, monkeypatch):
        monkeypatch.setenv("LINCHPIN_MEMORY_VERSION_RETENTION_DAYS", "0")
        assert memory_retention_days() == 1

    def test_retention_days_garbage_falls_back_to_30(self, monkeypatch):
        monkeypatch.setenv("LINCHPIN_MEMORY_VERSION_RETENTION_DAYS", "not-a-number")
        assert memory_retention_days() == 30

    def test_root_default(self, monkeypatch):
        monkeypatch.delenv("LINCHPIN_MEMORY_ROOT", raising=False)
        assert memory_store_root() == "/var/lib/linchpin/memory_stores"

    def test_root_overridden(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LINCHPIN_MEMORY_ROOT", str(tmp_path))
        assert memory_store_root() == str(tmp_path)


# ---------------------------------------------------------------------------
# PreconditionFailed exception shape
# ---------------------------------------------------------------------------


def test_precondition_failed_carries_expected_actual():
    exc = PreconditionFailed(expected="abc", actual="def")
    assert exc.expected == "abc"
    assert exc.actual == "def"
    assert "precondition failed" in str(exc).lower()


# ---------------------------------------------------------------------------
# Memory versions + redact (PR2)
# ---------------------------------------------------------------------------


def _make_version_row(
    *,
    ver_id: uuid.UUID | None = None,
    memory_id: uuid.UUID | None = None,
    store_id: uuid.UUID | None = None,
    seq: int = 1,
    content_sha256: str | None = None,
    size_bytes: int = 4,
    storage_path: str = "ab/cd/abcdef",
    author: str = "api",
    action: str = "create",
    redacted_at=None,
    expires_at=None,
):
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    return {
        "id": ver_id or uuid.uuid4(),
        "memory_id": memory_id or uuid.uuid4(),
        "memory_store_id": store_id or uuid.uuid4(),
        "seq": seq,
        "content_sha256": content_sha256 or ("a" * 64),
        "size_bytes": size_bytes,
        "storage_path": storage_path,
        "author": author,
        "action": action,
        "redacted_at": redacted_at,
        "created_at": now,
        "expires_at": expires_at or (now + timedelta(days=30)),
    }


@patch("app.routes.memory_versions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.memory_versions.fetch_one", new_callable=AsyncMock)
def test_list_versions_filters_by_memory_id(mock_fetch_one, mock_fetch_all, client):
    sid = uuid.uuid4()
    mid = uuid.uuid4()
    mock_fetch_one.return_value = {"id": sid}  # store exists
    mock_fetch_all.return_value = [
        _make_version_row(store_id=sid, memory_id=mid, seq=2),
        _make_version_row(store_id=sid, memory_id=mid, seq=1),
    ]
    resp = client.get(
        f"/v1/memory_stores/{sid}/memory_versions?memory_id={mid}",
        headers=AUTH,
    )
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 2
    # SQL filtered by memory_id — assert the parametrized query carried it
    sql_call = mock_fetch_all.await_args
    assert sql_call.args[1] == sid
    assert sql_call.args[2] == mid


@patch("app.routes.memory_versions.fetch_one", new_callable=AsyncMock)
def test_get_version_returns_metadata(mock_fetch, client):
    sid = uuid.uuid4()
    vid = uuid.uuid4()
    # Two fetch_one calls: store-exists check, then version row
    mock_fetch.side_effect = [
        {"id": sid},
        _make_version_row(ver_id=vid, store_id=sid),
    ]
    resp = client.get(
        f"/v1/memory_stores/{sid}/memory_versions/{vid}",
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == str(vid)


@patch("app.routes.memory_versions.fetch_one", new_callable=AsyncMock)
def test_get_version_content_404_when_redacted(mock_fetch, client):
    sid = uuid.uuid4()
    vid = uuid.uuid4()
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    mock_fetch.side_effect = [
        {"id": sid},
        _make_version_row(ver_id=vid, store_id=sid, redacted_at=now),
    ]
    resp = client.get(
        f"/v1/memory_stores/{sid}/memory_versions/{vid}/content",
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "redacted"


@patch("app.routes.memory_versions.fetch_one", new_callable=AsyncMock)
def test_get_version_content_204ish_for_delete_action(mock_fetch, client):
    """Delete-action versions have empty storage_path → empty body, 200."""
    sid = uuid.uuid4()
    vid = uuid.uuid4()
    mock_fetch.side_effect = [
        {"id": sid},
        _make_version_row(
            ver_id=vid, store_id=sid, action="delete", storage_path=""
        ),
    ]
    resp = client.get(
        f"/v1/memory_stores/{sid}/memory_versions/{vid}/content",
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.content == b""


@patch("app.routes.memory_versions.fetch_one", new_callable=AsyncMock)
@patch("app.routes.memory_versions.execute", new_callable=AsyncMock)
@patch("app.routes.memory_versions.LocalFileStore")
def test_redact_zeros_and_unlinks(
    mock_filestore_cls, mock_execute, mock_fetch, client
):
    sid = uuid.uuid4()
    vid = uuid.uuid4()
    mid = uuid.uuid4()
    target_row = _make_version_row(
        ver_id=vid, store_id=sid, memory_id=mid, seq=3, storage_path="aa/bb/aabb"
    )
    redacted_row = dict(target_row)
    redacted_row["redacted_at"] = datetime(2026, 5, 16, tzinfo=timezone.utc)
    redacted_row["storage_path"] = ""
    redacted_row["content_sha256"] = "0" * 64
    redacted_row["size_bytes"] = 0
    mock_fetch.side_effect = [
        {"id": sid},          # store exists
        target_row,           # original row
        redacted_row,         # RETURNING *
        {"max_seq": 3},       # head check — IS the head
    ]
    fs_inst = MagicMock()
    fs_inst.delete = AsyncMock()
    mock_filestore_cls.return_value = fs_inst

    resp = client.post(
        f"/v1/memory_stores/{sid}/memory_versions/{vid}/redact",
        json={"reason": "GDPR request"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["redacted_at"] is not None
    assert body["content_sha256"] == "0" * 64
    # FileStore.delete called with the original storage path
    fs_inst.delete.assert_awaited_once_with("aa/bb/aabb")
    # Two execute()s happened: the new redact version + memory soft-delete
    assert mock_execute.await_count == 2


@patch("app.routes.memory_versions.fetch_one", new_callable=AsyncMock)
@patch("app.routes.memory_versions.execute", new_callable=AsyncMock)
@patch("app.routes.memory_versions.LocalFileStore")
def test_redact_non_head_does_not_advance_head(
    mock_filestore_cls, mock_execute, mock_fetch, client
):
    """Redacting an older version doesn't touch the head — no
    advancing 'redact' row is written."""
    sid = uuid.uuid4()
    vid = uuid.uuid4()
    mid = uuid.uuid4()
    target_row = _make_version_row(
        ver_id=vid, store_id=sid, memory_id=mid, seq=2, storage_path="aa/bb/old"
    )
    redacted_row = dict(target_row)
    redacted_row["redacted_at"] = datetime(2026, 5, 16, tzinfo=timezone.utc)
    redacted_row["storage_path"] = ""
    redacted_row["content_sha256"] = "0" * 64
    redacted_row["size_bytes"] = 0
    mock_fetch.side_effect = [
        {"id": sid},
        target_row,
        redacted_row,
        {"max_seq": 5},       # head is 5; we just redacted seq=2
    ]
    fs_inst = MagicMock()
    fs_inst.delete = AsyncMock()
    mock_filestore_cls.return_value = fs_inst

    resp = client.post(
        f"/v1/memory_stores/{sid}/memory_versions/{vid}/redact",
        json={"reason": "old data"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    # No additional execute()s because head wasn't advanced
    assert mock_execute.await_count == 0


@patch("app.routes.memory_versions.fetch_one", new_callable=AsyncMock)
def test_redact_idempotent_on_already_redacted(mock_fetch, client):
    sid = uuid.uuid4()
    vid = uuid.uuid4()
    now = datetime(2026, 5, 15, tzinfo=timezone.utc)
    mock_fetch.side_effect = [
        {"id": sid},
        _make_version_row(ver_id=vid, store_id=sid, redacted_at=now),
    ]
    resp = client.post(
        f"/v1/memory_stores/{sid}/memory_versions/{vid}/redact",
        json={"reason": "second-time"},
        headers=AUTH,
    )
    # Idempotent — no error
    assert resp.status_code == 200
    assert resp.json()["redacted_at"] is not None


def test_memory_versions_endpoints_require_auth(client):
    sid = uuid.uuid4()
    vid = uuid.uuid4()
    assert client.get(f"/v1/memory_stores/{sid}/memory_versions").status_code == 401
    assert client.get(f"/v1/memory_stores/{sid}/memory_versions/{vid}").status_code == 401
    assert client.get(
        f"/v1/memory_stores/{sid}/memory_versions/{vid}/content"
    ).status_code == 401
    assert client.post(
        f"/v1/memory_stores/{sid}/memory_versions/{vid}/redact",
        json={"reason": "x"},
    ).status_code == 401
