"""Unit tests for session CRUD endpoints.

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.6, 6.5, 19.2, 19.3, 20.1, 20.2, 20.3
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


AUTH = {"Authorization": "Bearer test-secret-key"}


def _make_agent_row(*, agent_id: str | None = None):
    """Minimal agent row for FK validation."""
    return {
        "id": uuid.UUID(agent_id) if agent_id else uuid.uuid4(),
        "name": "test-agent",
        "version": 1,
        "model": {"provider": "openrouter", "id": "anthropic/claude-sonnet-4", "base_url": None},
        "system": "You are helpful.",
        "tools": [],
        "mcp_servers": [],
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


def _make_env_row(*, env_id: str | None = None, net_type: str = "none"):
    """Minimal environment row for FK validation."""
    return {
        "id": uuid.UUID(env_id) if env_id else uuid.uuid4(),
        "name": "test-env",
        "config": {"networking": {"type": net_type}},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


def _make_session_row(
    *,
    session_id: str | None = None,
    agent_id: str | None = None,
    environment_id: str | None = None,
    status: str = "running",
    container_id: str = "container-abc",
    title: str | None = None,
    metadata: dict | None = None,
    archived_at=None,
    vault_ids: list | None = None,
):
    """Build a fake asyncpg Record-like dict for a session row."""
    return {
        "id": uuid.UUID(session_id) if session_id else uuid.uuid4(),
        "agent_id": uuid.UUID(agent_id) if agent_id else uuid.uuid4(),
        "agent_version": 1,
        "environment_id": uuid.UUID(environment_id) if environment_id else uuid.uuid4(),
        "status": status,
        "container_id": container_id,
        "title": title,
        "metadata": metadata or {},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "archived_at": archived_at,
        "last_event_cursor": None,
        "ttl_seconds": None,
        "stats": {"total_events": 0, "tool_calls": 0, "model_turns": 0},
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        "vault_ids": vault_ids or [],
    }


@pytest.fixture()
def sandbox_client(tmp_path):
    """Return a TestClient with a mocked sandbox on app.state.

    PR5 — patches the deliverables watcher hooks so tests don't try to
    mkdir under /var/lib/linchpin or spin up real watchfiles tasks.
    """
    from pathlib import Path

    def _fake_ensure(sid):
        p = tmp_path / "session-outputs" / sid
        p.mkdir(parents=True, exist_ok=True)
        return p

    with (
        patch("app.main.check_migrations_current"),
        patch("app.main.create_pool", new_callable=AsyncMock),
        patch("app.main.close_pool", new_callable=AsyncMock),
        patch("app.main.ensure_docker_networks", new_callable=AsyncMock),
        patch("app.main.DockerSandbox") as MockSandbox,
        patch("app.main.cleanup_expired_sessions", new_callable=AsyncMock),
        patch("app.main.recover_sessions", new_callable=AsyncMock),
        patch("app.routes.sessions.run_session", new_callable=AsyncMock) as mock_run_session,
        patch("app.routes.sessions.ensure_session_outputs_dir", side_effect=_fake_ensure),
        patch("app.routes.sessions.watch_session_deliverables", new_callable=AsyncMock),
    ):
        mock_sandbox = MagicMock()
        mock_sandbox.create = AsyncMock(return_value="container-abc")
        mock_sandbox.destroy = AsyncMock()
        mock_sandbox.ensure_image = AsyncMock(
            side_effect=lambda *, base_image, packages: base_image
        )
        MockSandbox.return_value = mock_sandbox

        from app.main import app

        with TestClient(app) as c:
            yield c, mock_sandbox


# ---- POST /v1/sessions ----


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_returns_201(mock_fetch, sandbox_client):
    """Valid session creation returns 201 with full session resource."""
    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())

    agent_row = _make_agent_row(agent_id=agent_id)
    env_row = _make_env_row(env_id=env_id)
    session_row = _make_session_row(agent_id=agent_id, environment_id=env_id)

    # fetch_one is called: agent lookup, env lookup, INSERT RETURNING
    mock_fetch.side_effect = [agent_row, env_row, session_row]

    payload = {"agent_id": agent_id, "environment_id": env_id}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "running"
    assert body["agent_id"] == agent_id
    assert body["environment_id"] == env_id
    assert "id" in body
    assert "created_at" in body
    mock_sandbox.create.assert_called_once()


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_unrestricted_network(mock_fetch, sandbox_client):
    """Session with unrestricted env uses linchpin-open network."""
    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())

    agent_row = _make_agent_row(agent_id=agent_id)
    env_row = _make_env_row(env_id=env_id, net_type="unrestricted")
    session_row = _make_session_row(agent_id=agent_id, environment_id=env_id)

    mock_fetch.side_effect = [agent_row, env_row, session_row]

    payload = {"agent_id": agent_id, "environment_id": env_id}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201
    # PR3 — image + network as positional args. PR5 — mounts always contains
    # at least the writable /mnt/session/outputs bind; no resources means
    # exactly that one entry.
    args, kwargs = mock_sandbox.create.call_args
    # v0.2.0 item #3 — image is now resolved by sandbox.ensure_image() before
    # reaching create(). Test mock passes the base_image through unchanged.
    from app.sandbox import DEFAULT_BASE_IMAGE
    assert args == (DEFAULT_BASE_IMAGE, "linchpin-open")
    assert len(kwargs["mounts"]) == 1
    assert kwargs["mounts"][0].container_path == "/mnt/session/outputs"
    assert kwargs["mounts"][0].mode == "rw"


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_none_network(mock_fetch, sandbox_client):
    """Session with none networking env uses linchpin-none network."""
    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())

    agent_row = _make_agent_row(agent_id=agent_id)
    env_row = _make_env_row(env_id=env_id, net_type="none")
    session_row = _make_session_row(agent_id=agent_id, environment_id=env_id)

    mock_fetch.side_effect = [agent_row, env_row, session_row]

    payload = {"agent_id": agent_id, "environment_id": env_id}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201
    args, kwargs = mock_sandbox.create.call_args
    from app.sandbox import DEFAULT_BASE_IMAGE
    assert args == (DEFAULT_BASE_IMAGE, "linchpin-none")
    assert len(kwargs["mounts"]) == 1
    assert kwargs["mounts"][0].container_path == "/mnt/session/outputs"


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_limited_network(mock_fetch, sandbox_client):
    """v0.2.0 item #4 — limited mode routes to linchpin-limited network."""
    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())

    agent_row = _make_agent_row(agent_id=agent_id)
    # Bypass _make_env_row's simple net_type since limited needs sub-fields.
    env_row = {
        "id": uuid.UUID(env_id),
        "name": "limited-env",
        "config": {
            "networking": {
                "type": "limited",
                "allowed_hosts": ["api.github.com"],
                "allow_mcp_servers": True,
                "allow_package_managers": False,
            },
        },
        "created_at": datetime(2026, 5, 15, tzinfo=timezone.utc),
        "archived_at": None,
    }
    session_row = _make_session_row(agent_id=agent_id, environment_id=env_id)

    mock_fetch.side_effect = [agent_row, env_row, session_row]

    payload = {"agent_id": agent_id, "environment_id": env_id}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201
    args, kwargs = mock_sandbox.create.call_args
    from app.sandbox import DEFAULT_BASE_IMAGE
    assert args == (DEFAULT_BASE_IMAGE, "linchpin-limited")


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_agent_not_found(mock_fetch, sandbox_client):
    """Session creation with non-existent agent returns 404."""
    client, _ = sandbox_client
    mock_fetch.return_value = None

    payload = {"agent_id": str(uuid.uuid4()), "environment_id": str(uuid.uuid4())}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_env_not_found(mock_fetch, sandbox_client):
    """Session creation with non-existent environment returns 404."""
    client, _ = sandbox_client
    agent_row = _make_agent_row()
    # First call returns agent, second returns None (env not found)
    mock_fetch.side_effect = [agent_row, None]

    payload = {"agent_id": str(uuid.uuid4()), "environment_id": str(uuid.uuid4())}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 404
    assert "Environment" in resp.json()["detail"]["message"]


def test_create_session_missing_agent_id_returns_422(sandbox_client):
    """Missing agent_id returns 422."""
    client, _ = sandbox_client
    payload = {"environment_id": str(uuid.uuid4())}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)
    assert resp.status_code == 422


def test_create_session_missing_env_id_returns_422(sandbox_client):
    """Missing environment_id returns 422."""
    client, _ = sandbox_client
    payload = {"agent_id": str(uuid.uuid4())}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)
    assert resp.status_code == 422


# ---- GET /v1/sessions/{id} ----


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_get_session_returns_200(mock_fetch, mock_fetch_all, sandbox_client):
    """Fetching an existing session returns 200 with full resource."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    mock_fetch.return_value = _make_session_row(session_id=sid)
    mock_fetch_all.return_value = []

    resp = client.get(f"/v1/sessions/{sid}", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == sid
    assert "stats" in body
    assert "usage" in body


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_get_session_not_found(mock_fetch, sandbox_client):
    """Fetching a non-existent session returns 404."""
    client, _ = sandbox_client
    mock_fetch.return_value = None

    resp = client.get(f"/v1/sessions/{uuid.uuid4()}", headers=AUTH)

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


def test_get_session_invalid_uuid(sandbox_client):
    """Fetching with an invalid UUID returns 404."""
    client, _ = sandbox_client
    resp = client.get("/v1/sessions/not-a-uuid", headers=AUTH)
    assert resp.status_code == 404


# ---- GET /v1/sessions ----


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
def test_list_sessions_returns_paginated(mock_fetch, sandbox_client):
    """Listing sessions returns a paginated response."""
    client, _ = sandbox_client
    # fetch_all is called twice: once for sessions, once for session_resources batch
    mock_fetch.side_effect = [
        [_make_session_row(), _make_session_row()],
        [],
    ]

    resp = client.get("/v1/sessions", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["has_more"] is False


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
def test_list_sessions_has_more(mock_fetch, sandbox_client):
    """When more sessions exist than the limit, has_more is True."""
    client, _ = sandbox_client
    mock_fetch.side_effect = [
        [_make_session_row() for _ in range(3)],
        [],
    ]

    resp = client.get("/v1/sessions?limit=2", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["has_more"] is True


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
def test_list_sessions_filter_by_agent_id(mock_fetch, sandbox_client):
    """Listing sessions with agent_id filter passes it to the query."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    mock_fetch.side_effect = [
        [_make_session_row(agent_id=agent_id)],
        [],
    ]

    resp = client.get(f"/v1/sessions?agent_id={agent_id}", headers=AUTH)

    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 1


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
def test_list_sessions_empty(mock_fetch, sandbox_client):
    """Listing sessions when none exist returns empty list."""
    client, _ = sandbox_client
    mock_fetch.return_value = []

    resp = client.get("/v1/sessions", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["has_more"] is False


# ---- POST /v1/sessions/{id} (update) ----


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_update_session_title(mock_fetch, mock_fetch_all, sandbox_client):
    """Updating session title returns updated resource."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    existing = _make_session_row(session_id=sid)
    updated = {**existing, "title": "New Title"}

    mock_fetch.side_effect = [existing, updated]
    mock_fetch_all.return_value = []

    resp = client.post(f"/v1/sessions/{sid}", json={"title": "New Title"}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["title"] == "New Title"


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_update_session_metadata(mock_fetch, mock_fetch_all, sandbox_client):
    """Updating session metadata returns updated resource."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    existing = _make_session_row(session_id=sid)
    new_meta = {"key": "value"}
    updated = {**existing, "metadata": new_meta}

    mock_fetch.side_effect = [existing, updated]
    mock_fetch_all.return_value = []

    resp = client.post(f"/v1/sessions/{sid}", json={"metadata": new_meta}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["metadata"] == new_meta


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_update_session_not_found(mock_fetch, sandbox_client):
    """Updating a non-existent session returns 404."""
    client, _ = sandbox_client
    mock_fetch.return_value = None

    resp = client.post(f"/v1/sessions/{uuid.uuid4()}", json={"title": "x"}, headers=AUTH)

    assert resp.status_code == 404


# ---- DELETE /v1/sessions/{id} ----


@patch("app.routes.sessions.execute", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_terminate_session(mock_fetch, mock_fetch_all, mock_execute, sandbox_client):
    """Terminating a session sets status to terminated, transitions live
    session_resources to 'unmounted', and destroys the container."""
    client, mock_sandbox = sandbox_client
    sid = str(uuid.uuid4())
    existing = _make_session_row(session_id=sid)
    terminated = {**existing, "status": "terminated"}

    mock_fetch.side_effect = [existing, terminated]
    mock_fetch_all.return_value = []

    resp = client.delete(f"/v1/sessions/{sid}", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["status"] == "terminated"
    mock_sandbox.destroy.assert_called_once_with("container-abc")
    # PR3 — resource state must transition to 'unmounted' so DELETE /v1/files
    # no longer 409s on files that were only mounted in this terminated session.
    update_sql_calls = [
        c for c in mock_execute.call_args_list
        if "UPDATE session_resources" in (c.args[0] if c.args else "")
        and "state = 'unmounted'" in (c.args[0] if c.args else "")
    ]
    assert len(update_sql_calls) == 1, (
        f"terminate_session must UPDATE session_resources state to 'unmounted'; "
        f"got execute calls: {[c.args[0][:80] for c in mock_execute.call_args_list]}"
    )


@patch("app.routes.sessions.execute", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_terminate_session_removes_outputs_dir(
    mock_fetch, mock_fetch_all, mock_execute, sandbox_client, tmp_path, monkeypatch,
):
    """Terminate wipes /mnt/session/outputs/<sid> on the host so the writable
    bind doesn't leak up to LINCHPIN_DELIVERABLES_PER_SESSION_CAP_BYTES of
    disk per session forever."""
    client, _ = sandbox_client
    outputs_root = tmp_path / "session-outputs"
    monkeypatch.setenv("LINCHPIN_SESSION_OUTPUTS_ROOT", str(outputs_root))

    sid = str(uuid.uuid4())
    existing = _make_session_row(session_id=sid)
    terminated = {**existing, "status": "terminated"}
    mock_fetch.side_effect = [existing, terminated]
    mock_fetch_all.return_value = []

    # Pre-stage the per-session bind directory as if a watcher had been
    # mirroring deliverables into it during the session's life.
    session_dir = outputs_root / sid
    session_dir.mkdir(parents=True)
    (session_dir / "report.txt").write_bytes(b"agent deliverable")
    (session_dir / "subdir").mkdir()
    (session_dir / "subdir" / "nested.bin").write_bytes(b"\x00\x01\x02")
    assert session_dir.exists()

    resp = client.delete(f"/v1/sessions/{sid}", headers=AUTH)

    assert resp.status_code == 200
    assert not session_dir.exists(), "outputs_dir should be removed on terminate"


@patch("app.routes.sessions.execute", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_terminate_session_outputs_dir_already_gone(
    mock_fetch, mock_fetch_all, mock_execute, sandbox_client, tmp_path, monkeypatch,
):
    """Terminate is robust to the outputs directory never having been
    created (e.g., a session that crashed before the watcher boot scan)."""
    client, _ = sandbox_client
    monkeypatch.setenv("LINCHPIN_SESSION_OUTPUTS_ROOT", str(tmp_path / "session-outputs"))

    sid = str(uuid.uuid4())
    existing = _make_session_row(session_id=sid)
    terminated = {**existing, "status": "terminated"}
    mock_fetch.side_effect = [existing, terminated]
    mock_fetch_all.return_value = []

    # No pre-stage — outputs dir does not exist.
    resp = client.delete(f"/v1/sessions/{sid}", headers=AUTH)
    assert resp.status_code == 200


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_terminate_session_not_found(mock_fetch, sandbox_client):
    """Terminating a non-existent session returns 404."""
    client, _ = sandbox_client
    mock_fetch.return_value = None

    resp = client.delete(f"/v1/sessions/{uuid.uuid4()}", headers=AUTH)

    assert resp.status_code == 404


# ---- POST /v1/sessions/{id}/archive ----


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_archive_session(mock_fetch, mock_fetch_all, sandbox_client):
    """Archiving a session sets archived_at."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    existing = _make_session_row(session_id=sid)
    archived = {**existing, "archived_at": datetime(2025, 6, 1, tzinfo=timezone.utc)}

    mock_fetch.side_effect = [existing, archived]
    mock_fetch_all.return_value = []

    resp = client.post(f"/v1/sessions/{sid}/archive", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["archived_at"] is not None


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_archive_session_already_archived_returns_409(mock_fetch, sandbox_client):
    """Archiving an already-archived session returns 409."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    existing = _make_session_row(
        session_id=sid,
        archived_at=datetime(2025, 5, 1, tzinfo=timezone.utc),
    )
    mock_fetch.return_value = existing

    resp = client.post(f"/v1/sessions/{sid}/archive", headers=AUTH)

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "conflict"


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_archive_session_not_found(mock_fetch, sandbox_client):
    """Archiving a non-existent session returns 404."""
    client, _ = sandbox_client
    mock_fetch.return_value = None

    resp = client.post(f"/v1/sessions/{uuid.uuid4()}/archive", headers=AUTH)

    assert resp.status_code == 404


# ---- Auth required ----


def test_sessions_endpoints_require_auth(sandbox_client):
    """All session endpoints should return 401 without auth."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    assert client.get("/v1/sessions").status_code == 401
    assert client.get(f"/v1/sessions/{sid}").status_code == 401
    assert client.post("/v1/sessions", json={"agent_id": "a", "environment_id": "b"}).status_code == 401
    assert client.delete(f"/v1/sessions/{sid}").status_code == 401
    assert client.post(f"/v1/sessions/{sid}/archive").status_code == 401


# ---- POST /v1/sessions/{id}/events ----


@patch("app.routes.sessions.notify", new_callable=AsyncMock)
@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_post_events_returns_201(mock_fetch, mock_append, mock_notify, sandbox_client):
    """Posting valid events to a session returns 201 with created events."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    session_row = _make_session_row(session_id=sid)
    mock_fetch.return_value = session_row

    # Mock append_event to return an Event-like object
    from app.models import Event
    mock_append.return_value = Event(
        session_id=sid,
        cursor="MQ==",
        seq=1,
        type="user.message",
        payload={"content": "hello"},
        processed_at=None,
    )

    payload = {"events": [{"type": "user.message", "payload": {"content": "hello"}}]}
    resp = client.post(f"/v1/sessions/{sid}/events", json=payload, headers=AUTH)

    assert resp.status_code == 201
    body = resp.json()
    assert len(body) == 1
    assert body[0]["type"] == "user.message"
    assert body[0]["payload"] == {"content": "hello"}
    mock_append.assert_called_once_with(sid, "user.message", {"content": "hello"})
    mock_notify.assert_called_once()


@patch("app.routes.sessions.notify", new_callable=AsyncMock)
@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_post_events_batch(mock_fetch, mock_append, mock_notify, sandbox_client):
    """Posting multiple events in a batch returns all created events."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    session_row = _make_session_row(session_id=sid)
    mock_fetch.return_value = session_row

    from app.models import Event
    mock_append.side_effect = [
        Event(session_id=sid, cursor="MQ==", seq=1, type="user.message", payload={"content": "hi"}, processed_at=None),
        Event(session_id=sid, cursor="Mg==", seq=2, type="user.interrupt", payload={}, processed_at=None),
    ]

    payload = {
        "events": [
            {"type": "user.message", "payload": {"content": "hi"}},
            {"type": "user.interrupt", "payload": {}},
        ]
    }
    resp = client.post(f"/v1/sessions/{sid}/events", json=payload, headers=AUTH)

    assert resp.status_code == 201
    body = resp.json()
    assert len(body) == 2
    assert body[0]["type"] == "user.message"
    assert body[1]["type"] == "user.interrupt"
    assert mock_append.call_count == 2


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_post_events_session_not_found(mock_fetch, sandbox_client):
    """Posting events to a non-existent session returns 404."""
    client, _ = sandbox_client
    mock_fetch.return_value = None

    payload = {"events": [{"type": "user.message", "payload": {"content": "hello"}}]}
    resp = client.post(f"/v1/sessions/{uuid.uuid4()}/events", json=payload, headers=AUTH)

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_post_events_archived_session_returns_409(mock_fetch, sandbox_client):
    """Posting events to an archived session returns 409."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    session_row = _make_session_row(
        session_id=sid,
        archived_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    mock_fetch.return_value = session_row

    payload = {"events": [{"type": "user.message", "payload": {"content": "hello"}}]}
    resp = client.post(f"/v1/sessions/{sid}/events", json=payload, headers=AUTH)

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "conflict"


def test_post_events_invalid_event_type_returns_422(sandbox_client):
    """Posting an event with an invalid type returns 422 (Pydantic validation)."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())

    payload = {"events": [{"type": "invalid.type", "payload": {}}]}
    resp = client.post(f"/v1/sessions/{sid}/events", json=payload, headers=AUTH)

    assert resp.status_code == 422


def test_post_events_invalid_uuid_returns_404(sandbox_client):
    """Posting events with an invalid session UUID returns 404."""
    client, _ = sandbox_client

    payload = {"events": [{"type": "user.message", "payload": {}}]}
    resp = client.post("/v1/sessions/not-a-uuid/events", json=payload, headers=AUTH)

    assert resp.status_code == 404


@patch("app.routes.sessions.notify", new_callable=AsyncMock)
@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_post_events_triggers_notify(mock_fetch, mock_append, mock_notify, sandbox_client):
    """Posting events triggers LISTEN/NOTIFY on the session channel."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    session_row = _make_session_row(session_id=sid)
    mock_fetch.return_value = session_row

    from app.models import Event
    mock_append.return_value = Event(
        session_id=sid, cursor="MQ==", seq=1, type="user.message", payload={}, processed_at=None,
    )

    payload = {"events": [{"type": "user.message", "payload": {}}]}
    resp = client.post(f"/v1/sessions/{sid}/events", json=payload, headers=AUTH)

    assert resp.status_code == 201
    expected_channel = f"session_{sid.replace('-', '_')}"
    mock_notify.assert_called_once_with(expected_channel, "new_events")


def test_post_events_requires_auth(sandbox_client):
    """POST /v1/sessions/{id}/events requires authentication."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    payload = {"events": [{"type": "user.message", "payload": {}}]}
    resp = client.post(f"/v1/sessions/{sid}/events", json=payload)
    assert resp.status_code == 401


# ---- v0.2.0 item #12: GET /events ?types[]= ----


@patch("app.routes.sessions.get_events", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_get_events_passes_types_through(mock_fetch_one, mock_get_events, sandbox_client):
    """?types[]= reaches get_events with a parallel list when types are valid."""
    from app.models import PaginatedEventsResponse
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    mock_fetch_one.return_value = {"id": uuid.UUID(sid)}
    mock_get_events.return_value = PaginatedEventsResponse(events=[], next_cursor=None)

    resp = client.get(
        f"/v1/sessions/{sid}/events?types[]=agent.message&types[]=agent.tool_use",
        headers=AUTH,
    )
    assert resp.status_code == 200
    mock_get_events.assert_awaited_once()
    kwargs = mock_get_events.await_args.kwargs
    assert kwargs["types"] == ["agent.message", "agent.tool_use"]


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_get_events_422_on_unknown_type(mock_fetch_one, sandbox_client):
    """Unknown event type strings return 422 with the offender, not silent empty results."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    mock_fetch_one.return_value = {"id": uuid.UUID(sid)}

    resp = client.get(
        f"/v1/sessions/{sid}/events?types[]=nope.not.real&types[]=agent.message",
        headers=AUTH,
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_event_type"
    assert detail["invalid"] == ["nope.not.real"]


@patch("app.routes.sessions.get_events", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_get_events_no_types_param_unchanged(mock_fetch_one, mock_get_events, sandbox_client):
    """Without types[], the route passes types=None through (existing behavior preserved)."""
    from app.models import PaginatedEventsResponse
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    mock_fetch_one.return_value = {"id": uuid.UUID(sid)}
    mock_get_events.return_value = PaginatedEventsResponse(events=[], next_cursor=None)

    resp = client.get(f"/v1/sessions/{sid}/events", headers=AUTH)
    assert resp.status_code == 200
    kwargs = mock_get_events.await_args.kwargs
    assert kwargs["types"] is None


@patch("app.routes.sessions.get_events", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_get_events_empty_types_value_skips_filter(mock_fetch_one, mock_get_events, sandbox_client):
    """`?types[]=` (no value) is treated as no filter, matching the documented contract.

    Starlette parses an empty query value as `[""]`, not `None`. Without
    handling, `""` would fail EVENT_TYPES validation and surface a confusing
    422. Empty / whitespace-only entries are stripped to `None` instead.
    """
    from app.models import PaginatedEventsResponse
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    mock_fetch_one.return_value = {"id": uuid.UUID(sid)}
    mock_get_events.return_value = PaginatedEventsResponse(events=[], next_cursor=None)

    resp = client.get(f"/v1/sessions/{sid}/events?types[]=", headers=AUTH)
    assert resp.status_code == 200
    kwargs = mock_get_events.await_args.kwargs
    assert kwargs["types"] is None


# ---- Vault binding on session creation (Task 7.2) ----


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_with_valid_vault_ids(mock_fetch, sandbox_client):
    """Session creation with valid vault_ids stores them and returns them."""
    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    vault_id = str(uuid.uuid4())

    agent_row = _make_agent_row(agent_id=agent_id)
    env_row = _make_env_row(env_id=env_id)
    # Vault lookup: exists and not archived
    vault_row = {"id": uuid.UUID(vault_id), "archived_at": None}
    session_row = _make_session_row(
        agent_id=agent_id, environment_id=env_id, vault_ids=[vault_id]
    )

    # fetch_one calls: agent, env, vault check, INSERT RETURNING
    mock_fetch.side_effect = [agent_row, env_row, vault_row, session_row]

    payload = {"agent_id": agent_id, "environment_id": env_id, "vault_ids": [vault_id]}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201
    body = resp.json()
    assert body["vault_ids"] == [vault_id]


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_vault_not_found_returns_422(mock_fetch, sandbox_client):
    """Session creation with a non-existent vault_id returns 422."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    bad_vault_id = str(uuid.uuid4())

    agent_row = _make_agent_row(agent_id=agent_id)
    env_row = _make_env_row(env_id=env_id)

    # fetch_one calls: agent, env, vault check (None = not found)
    mock_fetch.side_effect = [agent_row, env_row, None]

    payload = {"agent_id": agent_id, "environment_id": env_id, "vault_ids": [bad_vault_id]}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 422
    assert "not found" in resp.json()["detail"]["message"].lower()


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_archived_vault_returns_422(mock_fetch, sandbox_client):
    """Session creation with an archived vault_id returns 422."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    vault_id = str(uuid.uuid4())

    agent_row = _make_agent_row(agent_id=agent_id)
    env_row = _make_env_row(env_id=env_id)
    archived_vault = {
        "id": uuid.UUID(vault_id),
        "archived_at": datetime(2025, 5, 1, tzinfo=timezone.utc),
    }

    # fetch_one calls: agent, env, vault check (archived)
    mock_fetch.side_effect = [agent_row, env_row, archived_vault]

    payload = {"agent_id": agent_id, "environment_id": env_id, "vault_ids": [vault_id]}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 422
    assert "archived" in resp.json()["detail"]["message"].lower()


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_invalid_vault_uuid_returns_422(mock_fetch, sandbox_client):
    """Session creation with an invalid vault UUID returns 422."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())

    agent_row = _make_agent_row(agent_id=agent_id)
    env_row = _make_env_row(env_id=env_id)

    # fetch_one calls: agent, env — vault validation fails before DB query
    mock_fetch.side_effect = [agent_row, env_row]

    payload = {"agent_id": agent_id, "environment_id": env_id, "vault_ids": ["not-a-uuid"]}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 422
    assert "invalid vault id" in resp.json()["detail"]["message"].lower()


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_empty_vault_ids_succeeds(mock_fetch, sandbox_client):
    """Session creation with empty vault_ids list succeeds normally."""
    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())

    agent_row = _make_agent_row(agent_id=agent_id)
    env_row = _make_env_row(env_id=env_id)
    session_row = _make_session_row(agent_id=agent_id, environment_id=env_id)

    mock_fetch.side_effect = [agent_row, env_row, session_row]

    payload = {"agent_id": agent_id, "environment_id": env_id, "vault_ids": []}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201
    assert resp.json()["vault_ids"] == []


def test_get_session_includes_vault_ids(sandbox_client):
    """GET session response includes vault_ids field."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    vault_id = str(uuid.uuid4())
    session_row = _make_session_row(session_id=sid, vault_ids=[vault_id])

    with (
        patch("app.routes.sessions.fetch_one", new_callable=AsyncMock, return_value=session_row),
        patch("app.routes.sessions.fetch_all", new_callable=AsyncMock, return_value=[]),
    ):
        resp = client.get(f"/v1/sessions/{sid}", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["vault_ids"] == [vault_id]


# ---- v0.3 PR6 — vault_ids → resources[] fold-in ----


@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_legacy_vault_ids_sets_deprecation_header(
    mock_fetch, mock_fetch_all, mock_append_event, sandbox_client
):
    """Sending the legacy ``vault_ids`` field marks the response with
    ``Linchpin-Deprecation: vault_ids`` and emits a
    ``session.deprecation_used`` event."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    vault_id = str(uuid.uuid4())

    mock_fetch.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        {"id": uuid.UUID(vault_id), "archived_at": None},
        _make_session_row(agent_id=agent_id, environment_id=env_id, vault_ids=[vault_id]),
    ]
    mock_fetch_all.return_value = []

    payload = {"agent_id": agent_id, "environment_id": env_id, "vault_ids": [vault_id]}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201, resp.text
    assert resp.headers.get("Linchpin-Deprecation") == "vault_ids"
    deprecation_events = [
        c for c in mock_append_event.await_args_list
        if c.args[1] == "session.deprecation_used"
    ]
    assert len(deprecation_events) == 1
    assert deprecation_events[0].args[2]["shape"] == "vault_ids"


@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_vault_resource_no_deprecation(
    mock_fetch, mock_fetch_all, mock_append_event, sandbox_client
):
    """Sending vault via the new ``resources[{type: vault, ...}]`` shape
    does NOT set the deprecation header — only the legacy ``vault_ids``
    field triggers it."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    vault_id = str(uuid.uuid4())

    mock_fetch.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        {"id": uuid.UUID(vault_id), "archived_at": None},
        _make_session_row(agent_id=agent_id, environment_id=env_id, vault_ids=[vault_id]),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [{"type": "vault", "vault_id": vault_id}],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201, resp.text
    assert resp.headers.get("Linchpin-Deprecation") is None
    assert resp.json()["vault_ids"] == [vault_id]
    deprecation_events = [
        c for c in mock_append_event.await_args_list
        if c.args[1] == "session.deprecation_used"
    ]
    assert deprecation_events == []


@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_both_shapes_dedupe_to_one_vault(
    mock_fetch, mock_fetch_all, mock_append_event, sandbox_client
):
    """Sending the same vault id in both ``vault_ids`` and
    ``resources[]`` dedupes to a single vault entry. Legacy field still
    triggers the deprecation signal."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    vault_id = str(uuid.uuid4())

    mock_fetch.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        {"id": uuid.UUID(vault_id), "archived_at": None},
        _make_session_row(agent_id=agent_id, environment_id=env_id, vault_ids=[vault_id]),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "vault_ids": [vault_id],
        "resources": [{"type": "vault", "vault_id": vault_id}],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201, resp.text
    # Header set because legacy field was present.
    assert resp.headers.get("Linchpin-Deprecation") == "vault_ids"
    # Exactly one vault lookup happened (3rd fetch_one in side_effect).
    vault_lookups = [
        c for c in mock_fetch.await_args_list
        if "FROM vaults" in c.args[0]
    ]
    assert len(vault_lookups) == 1
