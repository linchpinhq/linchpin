"""Tests for v0.6.0 PR2 — multi-agent threads.

The helper module's depth + root walkers are mocked-DB unit tests.
The route surface (``POST /v1/sessions/{id}/threads``,
``GET .../threads``) is covered against the existing test client
with subprocess + DB calls stubbed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.threads import (
    ThreadDepthExceeded,
    compute_depth,
    ensure_can_spawn_thread,
    max_thread_depth,
    root_session_id,
    thread_event_payload,
)


AUTH = {"Authorization": "Bearer test-secret-key"}


# ---------------------------------------------------------------------------
# max_thread_depth (env-driven)
# ---------------------------------------------------------------------------


class TestMaxThreadDepth:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("LINCHPIN_MAX_THREAD_DEPTH", raising=False)
        assert max_thread_depth() == 3

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("LINCHPIN_MAX_THREAD_DEPTH", "5")
        assert max_thread_depth() == 5

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("LINCHPIN_MAX_THREAD_DEPTH", "not-a-number")
        assert max_thread_depth() == 3

    def test_floor_is_one(self, monkeypatch):
        monkeypatch.setenv("LINCHPIN_MAX_THREAD_DEPTH", "0")
        assert max_thread_depth() == 1


# ---------------------------------------------------------------------------
# compute_depth / root_session_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_depth_root_is_zero():
    sid = uuid.uuid4()
    with patch("app.threads.fetch_one", new_callable=AsyncMock) as fetch_one:
        fetch_one.return_value = {"parent_session_id": None}
        assert await compute_depth(sid) == 0


@pytest.mark.asyncio
async def test_compute_depth_walks_chain():
    """root → t1 → t2 → t3 should report depth 3 for t3."""
    root, t1, t2, t3 = (uuid.uuid4() for _ in range(4))
    chain = {t3: t2, t2: t1, t1: root, root: None}
    with patch("app.threads.fetch_one", new_callable=AsyncMock) as fetch_one:
        async def _lookup(sql, sid):
            return {"parent_session_id": chain[sid]}
        fetch_one.side_effect = _lookup
        assert await compute_depth(t3) == 3


@pytest.mark.asyncio
async def test_compute_depth_caps_loop_at_32():
    """A pathological cycle in the data (shouldn't happen but defense in
    depth) returns at most 32 hops rather than running forever."""
    sid = uuid.uuid4()
    with patch("app.threads.fetch_one", new_callable=AsyncMock) as fetch_one:
        fetch_one.return_value = {"parent_session_id": sid}
        depth = await compute_depth(sid)
        assert depth == 32


@pytest.mark.asyncio
async def test_root_session_id_returns_topmost():
    root, t1, t2 = (uuid.uuid4() for _ in range(3))
    chain = {t2: t1, t1: root, root: None}
    with patch("app.threads.fetch_one", new_callable=AsyncMock) as fetch_one:
        async def _lookup(sql, sid):
            return {"parent_session_id": chain[sid]}
        fetch_one.side_effect = _lookup
        assert await root_session_id(t2) == root


# ---------------------------------------------------------------------------
# ensure_can_spawn_thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_can_spawn_thread_below_cap(monkeypatch):
    monkeypatch.setenv("LINCHPIN_MAX_THREAD_DEPTH", "3")
    sid = uuid.uuid4()
    with patch("app.threads.fetch_one", new_callable=AsyncMock) as fetch_one:
        fetch_one.return_value = {"parent_session_id": None}
        # parent at depth 0; child at depth 1; cap 3 → fine
        await ensure_can_spawn_thread(sid)


@pytest.mark.asyncio
async def test_ensure_can_spawn_thread_at_cap_rejects(monkeypatch):
    """Cap 3 means depth 0 → depth 1 → depth 2 is the deepest chain;
    spawning a child from a depth-2 parent breaches the cap."""
    monkeypatch.setenv("LINCHPIN_MAX_THREAD_DEPTH", "3")
    root, t1, t2 = (uuid.uuid4() for _ in range(3))
    chain = {t2: t1, t1: root, root: None}
    with patch("app.threads.fetch_one", new_callable=AsyncMock) as fetch_one:
        async def _lookup(sql, sid):
            return {"parent_session_id": chain[sid]}
        fetch_one.side_effect = _lookup
        with pytest.raises(ThreadDepthExceeded):
            await ensure_can_spawn_thread(t2)


# ---------------------------------------------------------------------------
# thread_event_payload
# ---------------------------------------------------------------------------


def test_thread_event_payload_tags_thread_id():
    assert thread_event_payload("th-1", {"content": "hi"}) == {
        "content": "hi", "thread_id": "th-1",
    }


def test_thread_event_payload_preserves_existing_tag():
    """A deeply-nested thread's event already has thread_id from the
    immediate child; we don't clobber it as it mirrors up the chain."""
    payload = {"content": "hi", "thread_id": "th-inner"}
    assert thread_event_payload("th-outer", payload) == payload


# ---------------------------------------------------------------------------
# Route surface
# ---------------------------------------------------------------------------


def _make_agent_row(*, agent_id=None):
    return {
        "id": uuid.UUID(agent_id) if agent_id else uuid.uuid4(),
        "name": "test-agent",
        "version": 1,
        "model": {"provider": "openrouter", "id": "claude", "base_url": None},
        "system": "You are helpful.",
        "tools": [],
        "mcp_servers": [],
        "skills": [],
        "description": None,
        "metadata": {},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "archived_at": None,
    }


def _make_env_row(*, env_id=None):
    return {
        "id": uuid.UUID(env_id) if env_id else uuid.uuid4(),
        "name": "test-env",
        "config": {"networking": {"type": "none"}},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


def _make_session_row(*, session_id=None, agent_id=None, environment_id=None, parent=None, archived=False):
    return {
        "id": uuid.UUID(session_id) if session_id else uuid.uuid4(),
        "agent_id": uuid.UUID(agent_id) if agent_id else uuid.uuid4(),
        "agent_version": 1,
        "environment_id": uuid.UUID(environment_id) if environment_id else uuid.uuid4(),
        "status": "running",
        "container_id": "container-abc",
        "title": None,
        "metadata": {},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "archived_at": datetime(2025, 1, 1, tzinfo=timezone.utc) if archived else None,
        "last_event_cursor": None,
        "ttl_seconds": None,
        "stats": {"total_events": 0, "tool_calls": 0, "model_turns": 0},
        "usage": {
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        },
        "vault_ids": [],
        "linchpin_api_version": None,
        "outcome": None,
        "parent_session_id": uuid.UUID(parent) if parent else None,
    }


@pytest.fixture()
def sandbox_client(tmp_path):
    fake_store = MagicMock()
    fake_store.absolute_path = lambda sp: f"/var/lib/linchpin/files/{sp}"

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
        patch("app.routes.sessions.run_session", new_callable=AsyncMock),
        patch("app.routes.sessions.get_file_store", return_value=fake_store),
        patch("app.routes.sessions.os.path.isfile", return_value=True),
        patch("app.routes.sessions.append_event", new_callable=AsyncMock),
        patch("app.routes.sessions.ensure_session_outputs_dir", side_effect=_fake_ensure),
        patch("app.routes.sessions.watch_session_deliverables", new_callable=AsyncMock),
        patch("app.routes.sessions.watch_memory_store", new_callable=AsyncMock),
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


@patch("app.routes.sessions.execute", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
@patch("app.threads.fetch_one", new_callable=AsyncMock)
def test_spawn_thread_creates_session_under_parent(
    mock_threads_fetch_one, mock_append, mock_fetch_one, mock_fetch_all, mock_execute, sandbox_client,
):
    client, _ = sandbox_client
    parent_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    thread_sid = str(uuid.uuid4())

    # threads.compute_depth is called before create_session, walks the
    # chain via app.threads.fetch_one (mocked separately from the route's).
    mock_threads_fetch_one.return_value = {"parent_session_id": None}

    # Route's fetch_one chain:
    # 1) parent existence + archived check
    # 2-5) create_session: agent, env, INSERT RETURNING, response hydrate
    mock_fetch_one.side_effect = [
        {"id": uuid.UUID(parent_id), "archived_at": None},
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_session_row(
            session_id=thread_sid, agent_id=agent_id, environment_id=env_id,
        ),
    ]
    mock_fetch_all.return_value = []

    resp = client.post(
        f"/v1/sessions/{parent_id}/threads",
        json={
            "agent_id": agent_id,
            "environment_id": env_id,
            "input_message": "Please review this PR.",
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["parent_session_id"] == parent_id
    # parent_session_id was stamped after create.
    update_calls = [
        c.args[0] for c in mock_execute.await_args_list
        if "parent_session_id" in c.args[0]
    ]
    assert update_calls

    # session.thread_created emitted on the parent.
    types = [c.args[1] for c in mock_append.await_args_list]
    assert "session.thread_created" in types
    # input_message seeded as user.message on the new thread.
    assert "user.message" in types


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_spawn_thread_parent_not_found_returns_404(mock_fetch_one, sandbox_client):
    client, _ = sandbox_client
    mock_fetch_one.return_value = None
    resp = client.post(
        f"/v1/sessions/{uuid.uuid4()}/threads",
        json={"agent_id": str(uuid.uuid4()), "environment_id": str(uuid.uuid4())},
        headers=AUTH,
    )
    assert resp.status_code == 404


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_spawn_thread_parent_archived_returns_409(mock_fetch_one, sandbox_client):
    client, _ = sandbox_client
    parent_id = str(uuid.uuid4())
    mock_fetch_one.return_value = {
        "id": uuid.UUID(parent_id),
        "archived_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    resp = client.post(
        f"/v1/sessions/{parent_id}/threads",
        json={"agent_id": str(uuid.uuid4()), "environment_id": str(uuid.uuid4())},
        headers=AUTH,
    )
    assert resp.status_code == 409


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
@patch("app.threads.fetch_one", new_callable=AsyncMock)
def test_spawn_thread_depth_cap_returns_422(
    mock_threads_fetch_one, mock_fetch_one, sandbox_client, monkeypatch,
):
    monkeypatch.setenv("LINCHPIN_MAX_THREAD_DEPTH", "3")
    client, _ = sandbox_client
    parent_id = str(uuid.uuid4())
    grandparent_id = str(uuid.uuid4())
    great_grandparent_id = str(uuid.uuid4())

    mock_fetch_one.return_value = {
        "id": uuid.UUID(parent_id), "archived_at": None,
    }
    # Parent's ancestors form a depth-2 chain → spawning a depth-3 child
    # breaches cap.
    chain = {
        uuid.UUID(parent_id): uuid.UUID(grandparent_id),
        uuid.UUID(grandparent_id): uuid.UUID(great_grandparent_id),
        uuid.UUID(great_grandparent_id): None,
    }

    async def _lookup(sql, sid):
        return {"parent_session_id": chain.get(sid)}

    mock_threads_fetch_one.side_effect = _lookup

    resp = client.post(
        f"/v1/sessions/{parent_id}/threads",
        json={"agent_id": str(uuid.uuid4()), "environment_id": str(uuid.uuid4())},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "thread_depth_exceeded"


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_list_threads_returns_children(mock_fetch_one, mock_fetch_all, sandbox_client):
    client, _ = sandbox_client
    parent_id = str(uuid.uuid4())
    mock_fetch_one.return_value = {"id": uuid.UUID(parent_id)}
    mock_fetch_all.return_value = [
        _make_session_row(parent=parent_id),
        _make_session_row(parent=parent_id),
    ]
    resp = client.get(f"/v1/sessions/{parent_id}/threads", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data) == 2
    assert all(d["parent_session_id"] == parent_id for d in data)


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_list_threads_parent_not_found_returns_404(mock_fetch_one, sandbox_client):
    client, _ = sandbox_client
    mock_fetch_one.return_value = None
    resp = client.get(f"/v1/sessions/{uuid.uuid4()}/threads", headers=AUTH)
    assert resp.status_code == 404
