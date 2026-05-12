"""Unit tests for agent CRUD endpoints.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


AUTH = {"Authorization": "Bearer test-secret-key"}


def _make_agent_row(
    *,
    agent_id: str | None = None,
    name: str = "test-agent",
    version: int = 1,
    model: dict | None = None,
    system: str = "You are helpful.",
    tools: list | None = None,
    mcp_servers: list | None = None,
):
    """Build a fake asyncpg Record-like dict for an agent row."""
    return {
        "id": uuid.UUID(agent_id) if agent_id else uuid.uuid4(),
        "name": name,
        "version": version,
        "model": model or {"provider": "openrouter", "id": "anthropic/claude-sonnet-4", "base_url": None},
        "system": system,
        "tools": tools or [],
        "mcp_servers": mcp_servers or [],
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


VALID_PAYLOAD = {
    "name": "my-agent",
    "model": {"provider": "openrouter", "id": "anthropic/claude-sonnet-4"},
    "system": "You are a helpful assistant.",
    "tools": [],
    "mcp_servers": [],
}


# ---- POST /v1/agents ----


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_create_agent_returns_201(mock_fetch, client):
    """A valid creation request returns 201 with the full agent resource."""
    mock_fetch.return_value = _make_agent_row(name="my-agent")

    resp = client.post("/v1/agents", json=VALID_PAYLOAD, headers=AUTH)

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "my-agent"
    assert body["version"] == 1
    assert body["model"]["provider"] == "openrouter"
    assert "id" in body
    assert "created_at" in body


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_create_agent_with_tools(mock_fetch, client):
    """Agent creation with tools stores them correctly."""
    tools = [
        {"type": "builtin", "default_config": {}, "configs": [
            {"name": "bash", "permission_policy": "always_allow", "enabled": True}
        ]},
    ]
    mock_fetch.return_value = _make_agent_row(tools=tools)

    payload = {**VALID_PAYLOAD, "tools": tools}
    resp = client.post("/v1/agents", json=payload, headers=AUTH)

    assert resp.status_code == 201
    assert len(resp.json()["tools"]) == 1


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_create_agent_with_mcp_servers(mock_fetch, client):
    """Agent creation with MCP servers stores them correctly."""
    mcp = [{"name": "my-mcp", "command": "npx", "args": ["-y", "server"], "env": {"KEY": "val"}}]
    mock_fetch.return_value = _make_agent_row(mcp_servers=mcp)

    payload = {**VALID_PAYLOAD, "mcp_servers": mcp}
    resp = client.post("/v1/agents", json=payload, headers=AUTH)

    assert resp.status_code == 201
    assert resp.json()["mcp_servers"][0]["name"] == "my-mcp"


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_create_agent_ollama_with_base_url(mock_fetch, client):
    """Ollama provider accepts optional base_url."""
    model = {"provider": "ollama", "id": "llama3", "base_url": "http://localhost:11434"}
    mock_fetch.return_value = _make_agent_row(model=model)

    payload = {**VALID_PAYLOAD, "model": model}
    resp = client.post("/v1/agents", json=payload, headers=AUTH)

    assert resp.status_code == 201
    assert resp.json()["model"]["base_url"] == "http://localhost:11434"


def test_create_agent_invalid_provider_returns_422(client):
    """Invalid model provider should be rejected with 422."""
    payload = {**VALID_PAYLOAD, "model": {"provider": "invalid", "id": "x"}}
    resp = client.post("/v1/agents", json=payload, headers=AUTH)
    assert resp.status_code == 422


def test_create_agent_missing_name_returns_422(client):
    """Missing required field 'name' should be rejected with 422."""
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "name"}
    resp = client.post("/v1/agents", json=payload, headers=AUTH)
    assert resp.status_code == 422


def test_create_agent_missing_system_returns_422(client):
    """Missing required field 'system' should be rejected with 422."""
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "system"}
    resp = client.post("/v1/agents", json=payload, headers=AUTH)
    assert resp.status_code == 422


def test_create_agent_missing_model_returns_422(client):
    """Missing required field 'model' should be rejected with 422."""
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "model"}
    resp = client.post("/v1/agents", json=payload, headers=AUTH)
    assert resp.status_code == 422


def test_create_agent_invalid_tool_permission_returns_422(client):
    """Tool with invalid permission_policy should be rejected with 422."""
    payload = {
        **VALID_PAYLOAD,
        "tools": [{"type": "builtin", "configs": [{"name": "bash", "permission_policy": "yolo"}]}],
    }
    resp = client.post("/v1/agents", json=payload, headers=AUTH)
    assert resp.status_code == 422


# ---- GET /v1/agents/{id} ----


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_get_agent_returns_200(mock_fetch, client):
    """Fetching an existing agent returns 200 with the full resource."""
    aid = str(uuid.uuid4())
    mock_fetch.return_value = _make_agent_row(agent_id=aid)

    resp = client.get(f"/v1/agents/{aid}", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["id"] == aid


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_get_agent_not_found_returns_404(mock_fetch, client):
    """Fetching a non-existent agent returns 404."""
    mock_fetch.return_value = None
    aid = str(uuid.uuid4())

    resp = client.get(f"/v1/agents/{aid}", headers=AUTH)

    assert resp.status_code == 404
    body = resp.json()["detail"]
    assert body["error"] == "not_found"


def test_get_agent_invalid_uuid_returns_404(client):
    """Fetching with an invalid UUID returns 404."""
    resp = client.get("/v1/agents/not-a-uuid", headers=AUTH)
    assert resp.status_code == 404


# ---- PATCH /v1/agents/{id} ----


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_update_agent_name_returns_200(mock_fetch, client):
    """Updating only the name returns 200 with bumped version."""
    aid = str(uuid.uuid4())
    mock_fetch.return_value = _make_agent_row(agent_id=aid, name="updated-name", version=2)

    resp = client.patch(f"/v1/agents/{aid}", json={"name": "updated-name"}, headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "updated-name"
    assert body["version"] == 2


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_update_agent_model_returns_200(mock_fetch, client):
    """Updating the model config returns 200."""
    aid = str(uuid.uuid4())
    new_model = {"provider": "openrouter", "id": "openai/gpt-4o", "base_url": None}
    mock_fetch.return_value = _make_agent_row(agent_id=aid, model=new_model, version=2)

    resp = client.patch(
        f"/v1/agents/{aid}",
        json={"model": {"provider": "openrouter", "id": "openai/gpt-4o"}},
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json()["model"]["provider"] == "openrouter"
    assert resp.json()["model"]["id"] == "openai/gpt-4o"


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_update_agent_system_returns_200(mock_fetch, client):
    """Updating the system prompt returns 200."""
    aid = str(uuid.uuid4())
    mock_fetch.return_value = _make_agent_row(agent_id=aid, system="New prompt", version=2)

    resp = client.patch(
        f"/v1/agents/{aid}",
        json={"system": "New prompt"},
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json()["system"] == "New prompt"


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_update_agent_not_found_returns_404(mock_fetch, client):
    """Updating a non-existent agent returns 404."""
    mock_fetch.return_value = None
    aid = str(uuid.uuid4())

    resp = client.patch(f"/v1/agents/{aid}", json={"name": "x"}, headers=AUTH)

    assert resp.status_code == 404
    body = resp.json()["detail"]
    assert body["error"] == "not_found"


def test_update_agent_invalid_uuid_returns_404(client):
    """Updating with an invalid UUID returns 404."""
    resp = client.patch("/v1/agents/not-a-uuid", json={"name": "x"}, headers=AUTH)
    assert resp.status_code == 404


@patch("app.routes.agents.fetch_one", new_callable=AsyncMock)
def test_update_agent_empty_body_bumps_version(mock_fetch, client):
    """An empty update body still bumps the version."""
    aid = str(uuid.uuid4())
    mock_fetch.return_value = _make_agent_row(agent_id=aid, version=2)

    resp = client.patch(f"/v1/agents/{aid}", json={}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["version"] == 2


# ---- GET /v1/agents ----


@patch("app.routes.agents.fetch_all", new_callable=AsyncMock)
def test_list_agents_returns_paginated(mock_fetch, client):
    """Listing agents returns a paginated response."""
    mock_fetch.return_value = [_make_agent_row(), _make_agent_row()]

    resp = client.get("/v1/agents", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["has_more"] is False


@patch("app.routes.agents.fetch_all", new_callable=AsyncMock)
def test_list_agents_has_more(mock_fetch, client):
    """When more agents exist than the limit, has_more is True."""
    # Request limit=2, return 3 rows (one extra signals has_more)
    mock_fetch.return_value = [_make_agent_row() for _ in range(3)]

    resp = client.get("/v1/agents?limit=2", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["has_more"] is True


@patch("app.routes.agents.fetch_all", new_callable=AsyncMock)
def test_list_agents_empty(mock_fetch, client):
    """Listing agents when none exist returns empty list."""
    mock_fetch.return_value = []

    resp = client.get("/v1/agents", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["has_more"] is False


# ---- Auth required ----


def test_agents_endpoints_require_auth(client):
    """All agent endpoints should return 401 without auth."""
    assert client.get("/v1/agents").status_code == 401
    assert client.get(f"/v1/agents/{uuid.uuid4()}").status_code == 401
    assert client.post("/v1/agents", json=VALID_PAYLOAD).status_code == 401
    assert client.patch(f"/v1/agents/{uuid.uuid4()}", json={"name": "x"}).status_code == 401
