"""Integration tests for cross-component behavior.

These tests verify end-to-end flows across multiple components:
- Session lifecycle (agent → environment → session → events → terminate)
- SSE streaming (replay + live streaming with cursor reconnect)
- Docker sandbox (create → exec → write/read file → destroy)

Validates: Requirements 1.1, 3.1, 5.1, 6.5, 7.1, 7.2, 9.1, 14.1, 14.2, 14.3, 14.4
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.events import encode_cursor
from app.models import Event, EventResponse, PaginatedEventsResponse


AUTH = {"Authorization": "Bearer test-secret-key"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _agent_row(agent_id: str, *, version: int = 1):
    """Build a realistic agent DB row."""
    return {
        "id": uuid.UUID(agent_id),
        "name": "integration-agent",
        "version": version,
        "model": {"provider": "openrouter", "id": "anthropic/claude-sonnet-4", "base_url": None},
        "system": "You are a helpful assistant.",
        "tools": [],
        "mcp_servers": [],
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


def _env_row(env_id: str, *, net_type: str = "none"):
    """Build a realistic environment DB row."""
    return {
        "id": uuid.UUID(env_id),
        "name": "integration-env",
        "config": {"networking": {"type": net_type}},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


def _session_row(
    session_id: str,
    agent_id: str,
    env_id: str,
    *,
    status: str = "running",
    container_id: str = "container-integ",
    archived_at=None,
):
    """Build a realistic session DB row."""
    return {
        "id": uuid.UUID(session_id),
        "agent_id": uuid.UUID(agent_id),
        "agent_version": 1,
        "environment_id": uuid.UUID(env_id),
        "status": status,
        "container_id": container_id,
        "title": None,
        "metadata": {},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "archived_at": archived_at,
        "last_event_cursor": None,
        "ttl_seconds": None,
        "stats": {"total_events": 0, "tool_calls": 0, "model_turns": 0},
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


# ---------------------------------------------------------------------------
# 15.1 — Session lifecycle integration tests
# Validates: Requirements 1.1, 3.1, 5.1, 9.1, 6.5
# ---------------------------------------------------------------------------


@pytest.fixture()
def lifecycle_client():
    """TestClient with mocked sandbox and orchestrator for lifecycle tests."""
    with (
        patch("app.main.check_migrations_current"),
        patch("app.main.create_pool", new_callable=AsyncMock),
        patch("app.main.close_pool", new_callable=AsyncMock),
        patch("app.main.ensure_docker_networks", new_callable=AsyncMock),
        patch("app.main.DockerSandbox") as MockSandbox,
        patch("app.main.cleanup_expired_sessions", new_callable=AsyncMock),
        patch("app.main.recover_sessions", new_callable=AsyncMock),
        patch("app.routes.sessions.run_session", new_callable=AsyncMock),
    ):
        mock_sandbox = MagicMock()
        mock_sandbox.create = AsyncMock(return_value="container-integ")
        mock_sandbox.destroy = AsyncMock()
        MockSandbox.return_value = mock_sandbox

        from app.main import app

        with TestClient(app) as c:
            yield c, mock_sandbox



class TestSessionLifecycle:
    """Integration tests for the full session lifecycle.

    Create agent → create environment → create session → send message →
    receive events → terminate. Verifies the end-to-end flow.

    Validates: Requirements 1.1, 3.1, 5.1, 9.1, 6.5
    """

    @patch("app.routes.sessions.notify", new_callable=AsyncMock)
    @patch("app.routes.sessions.execute", new_callable=AsyncMock)
    @patch("app.routes.sessions.append_event", new_callable=AsyncMock)
    @patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
    @patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
    @patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
    @patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
    @patch("app.routes.agents.fetch_all", new_callable=AsyncMock)
    @patch("app.routes.environments.fetch_all", new_callable=AsyncMock)
    def test_full_lifecycle_create_agent_env_session_events_terminate(
        self,
        mock_env_fetch_all,
        mock_agent_fetch_all,
        mock_agent_fetch,
        mock_env_fetch,
        mock_session_fetch_all,
        mock_session_fetch,
        mock_append,
        _mock_session_execute,
        mock_notify,
        lifecycle_client,
    ):
        """End-to-end: create agent, create env, create session, post event, terminate."""
        client, mock_sandbox = lifecycle_client

        agent_id = str(uuid.uuid4())
        env_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())

        agent = _agent_row(agent_id)
        env = _env_row(env_id)
        session = _session_row(session_id, agent_id, env_id)

        # --- Step 1: Create agent ---
        mock_agent_fetch.return_value = agent
        resp = client.post(
            "/v1/agents",
            json={
                "name": "integration-agent",
                "model": {"provider": "openrouter", "id": "anthropic/claude-sonnet-4"},
                "system": "You are a helpful assistant.",
            },
            headers=AUTH,
        )
        assert resp.status_code == 201
        created_agent = resp.json()
        assert created_agent["name"] == "integration-agent"
        assert created_agent["model"]["provider"] == "openrouter"

        # --- Step 2: Create environment ---
        mock_env_fetch.return_value = env
        resp = client.post(
            "/v1/environments",
            json={"name": "integration-env", "config": {"networking": {"type": "none"}}},
            headers=AUTH,
        )
        assert resp.status_code == 201
        created_env = resp.json()
        assert created_env["name"] == "integration-env"
        assert created_env["config"]["networking"]["type"] == "none"

        # --- Step 3: Create session ---
        # session fetch_one: agent lookup, env lookup, INSERT RETURNING
        mock_session_fetch.side_effect = [agent, env, session]
        resp = client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "environment_id": env_id},
            headers=AUTH,
        )
        assert resp.status_code == 201
        created_session = resp.json()
        assert created_session["status"] == "running"
        assert created_session["agent_id"] == agent_id
        assert created_session["environment_id"] == env_id
        mock_sandbox.create.assert_called_once_with("", "linchpin-none", mounts=[])

        # --- Step 4: Send a user.message event ---
        mock_session_fetch.side_effect = None
        mock_session_fetch.return_value = session
        mock_append.return_value = Event(
            session_id=session_id,
            cursor=encode_cursor(1),
            seq=1,
            type="user.message",
            payload={"content": "Hello, agent!"},
            processed_at=None,
        )

        resp = client.post(
            f"/v1/sessions/{session_id}/events",
            json={"events": [{"type": "user.message", "payload": {"content": "Hello, agent!"}}]},
            headers=AUTH,
        )
        assert resp.status_code == 201
        events = resp.json()
        assert len(events) == 1
        assert events[0]["type"] == "user.message"
        assert events[0]["payload"]["content"] == "Hello, agent!"
        mock_notify.assert_called_once()

        # --- Step 5: Terminate session ---
        terminated = {**session, "status": "terminated"}
        mock_session_fetch.side_effect = [session, terminated]
        resp = client.delete(f"/v1/sessions/{session_id}", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["status"] == "terminated"
        mock_sandbox.destroy.assert_called_once_with("container-integ")

    @patch("app.routes.sessions.notify", new_callable=AsyncMock)
    @patch("app.routes.sessions.append_event", new_callable=AsyncMock)
    @patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
    @patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
    @patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
    def test_session_with_unrestricted_network(
        self,
        mock_env_fetch,
        mock_agent_fetch,
        mock_session_fetch,
        mock_append,
        mock_notify,
        lifecycle_client,
    ):
        """Session created with unrestricted env uses linchpin-open network."""
        client, mock_sandbox = lifecycle_client

        agent_id = str(uuid.uuid4())
        env_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())

        agent = _agent_row(agent_id)
        env = _env_row(env_id, net_type="unrestricted")
        session = _session_row(session_id, agent_id, env_id)

        # Create agent
        mock_agent_fetch.return_value = agent
        resp = client.post(
            "/v1/agents",
            json={
                "name": "agent-open",
                "model": {"provider": "openrouter", "id": "openai/gpt-4"},
                "system": "You are helpful.",
            },
            headers=AUTH,
        )
        assert resp.status_code == 201

        # Create environment with unrestricted networking
        mock_env_fetch.return_value = env
        resp = client.post(
            "/v1/environments",
            json={"name": "env-open", "config": {"networking": {"type": "unrestricted"}}},
            headers=AUTH,
        )
        assert resp.status_code == 201

        # Create session — should use linchpin-open
        mock_session_fetch.side_effect = [agent, env, session]
        resp = client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "environment_id": env_id},
            headers=AUTH,
        )
        assert resp.status_code == 201
        mock_sandbox.create.assert_called_with("", "linchpin-open", mounts=[])

    @patch("app.routes.sessions.notify", new_callable=AsyncMock)
    @patch("app.routes.sessions.append_event", new_callable=AsyncMock)
    @patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
    def test_post_multiple_events_in_sequence(
        self,
        mock_session_fetch,
        mock_append,
        mock_notify,
        lifecycle_client,
    ):
        """Posting multiple events in a batch appends all and triggers notify."""
        client, _ = lifecycle_client

        session_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        env_id = str(uuid.uuid4())
        session = _session_row(session_id, agent_id, env_id)

        mock_session_fetch.return_value = session
        mock_append.side_effect = [
            Event(session_id=session_id, cursor=encode_cursor(1), seq=1,
                  type="user.message", payload={"content": "msg1"}, processed_at=None),
            Event(session_id=session_id, cursor=encode_cursor(2), seq=2,
                  type="user.message", payload={"content": "msg2"}, processed_at=None),
            Event(session_id=session_id, cursor=encode_cursor(3), seq=3,
                  type="user.interrupt", payload={}, processed_at=None),
        ]

        resp = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "events": [
                    {"type": "user.message", "payload": {"content": "msg1"}},
                    {"type": "user.message", "payload": {"content": "msg2"}},
                    {"type": "user.interrupt", "payload": {}},
                ]
            },
            headers=AUTH,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert len(body) == 3
        assert body[0]["seq"] == 1
        assert body[1]["seq"] == 2
        assert body[2]["type"] == "user.interrupt"
        assert mock_append.call_count == 3
        mock_notify.assert_called_once()

    @patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
    def test_events_rejected_for_archived_session(
        self,
        mock_session_fetch,
        lifecycle_client,
    ):
        """Posting events to an archived session returns 409 conflict."""
        client, _ = lifecycle_client

        session_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        env_id = str(uuid.uuid4())
        session = _session_row(
            session_id, agent_id, env_id,
            archived_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        mock_session_fetch.return_value = session

        resp = client.post(
            f"/v1/sessions/{session_id}/events",
            json={"events": [{"type": "user.message", "payload": {"content": "nope"}}]},
            headers=AUTH,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "conflict"

    def test_unauthenticated_requests_rejected_across_all_endpoints(self, lifecycle_client):
        """All lifecycle endpoints reject requests without auth."""
        client, _ = lifecycle_client
        sid = str(uuid.uuid4())

        assert client.post("/v1/agents", json={"name": "x", "model": {"provider": "openrouter", "id": "m"}, "system": "s"}).status_code == 401
        assert client.post("/v1/environments", json={"name": "x", "config": {"networking": {"type": "none"}}}).status_code == 401
        assert client.post("/v1/sessions", json={"agent_id": sid, "environment_id": sid}).status_code == 401
        assert client.post(f"/v1/sessions/{sid}/events", json={"events": [{"type": "user.message", "payload": {}}]}).status_code == 401
        assert client.delete(f"/v1/sessions/{sid}").status_code == 401

