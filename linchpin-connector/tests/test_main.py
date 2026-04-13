"""Tests for linchpin-connector /tools/invoke endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# MCP tool invocation — no server running → error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_mcp_tool_no_server_returns_error(client):
    payload = {
        "session_id": "sess-1",
        "tool_type": "mcp",
        "server_name": "my-mcp-server",
        "tool_name": "list_files",
        "arguments": {"path": "/tmp"},
        "credentials": {"API_KEY": "secret"},
    }
    resp = await client.post("/tools/invoke", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "not found" in body["error"].lower()


# ---------------------------------------------------------------------------
# MCP tool invocation — missing server_name
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_mcp_tool_missing_server_name(client):
    payload = {
        "session_id": "sess-1",
        "tool_type": "mcp",
        "tool_name": "list_files",
    }
    resp = await client.post("/tools/invoke", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "server_name" in body["error"].lower()


# ---------------------------------------------------------------------------
# Custom HTTP tool invocation — missing endpoint → error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_custom_http_tool_missing_endpoint(client):
    payload = {
        "session_id": "sess-2",
        "tool_type": "custom_http",
        "tool_name": "weather_lookup",
        "arguments": {"city": "Seattle"},
    }
    resp = await client.post("/tools/invoke", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert "endpoint" in body["error"].lower()


# ---------------------------------------------------------------------------
# Custom HTTP tool invocation — success via mock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_custom_http_tool_success(client):
    with patch("app.main.invoke_http_tool", new_callable=AsyncMock) as mock_invoke:
        mock_invoke.return_value = {"result": {"temp": 72}, "status_code": 200}

        payload = {
            "session_id": "sess-2",
            "tool_type": "custom_http",
            "tool_name": "weather_lookup",
            "arguments": {"city": "Seattle"},
            "endpoint": "https://example.com/weather",
        }
        resp = await client.post("/tools/invoke", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["result"] == {"temp": 72}


# ---------------------------------------------------------------------------
# Validation — invalid tool_type rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_invalid_tool_type_returns_422(client):
    payload = {
        "session_id": "sess-3",
        "tool_type": "unknown",
        "tool_name": "something",
    }
    resp = await client.post("/tools/invoke", json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Validation — missing required fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_missing_session_id_returns_422(client):
    payload = {
        "tool_type": "mcp",
        "tool_name": "something",
    }
    resp = await client.post("/tools/invoke", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_invoke_missing_tool_name_returns_422(client):
    payload = {
        "session_id": "sess-4",
        "tool_type": "mcp",
    }
    resp = await client.post("/tools/invoke", json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# MCP minimal payload — no server_name → error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_mcp_minimal_payload(client):
    payload = {
        "session_id": "sess-5",
        "tool_type": "mcp",
        "tool_name": "ping",
    }
    resp = await client.post("/tools/invoke", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"


# ---------------------------------------------------------------------------
# Custom HTTP minimal payload — no endpoint → error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_custom_http_minimal_payload(client):
    payload = {
        "session_id": "sess-6",
        "tool_type": "custom_http",
        "tool_name": "do_thing",
    }
    resp = await client.post("/tools/invoke", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
