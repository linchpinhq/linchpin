"""Tests for the URL MCP transport (v0.5.0 PR2).

Mocks out the upstream HTTP server via httpx.MockTransport so we can
pin: header injection, JSON-RPC framing, SSE handling, error
surfaces, and per-(session, server) client lifecycle.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.mcp_remote import MCPRemoteManager


def _json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"content-type": "application/json"},
        json=payload,
    )


def _sse_response(payload: dict, status: int = 200) -> httpx.Response:
    body = f"event: message\ndata: {json.dumps(payload)}\n\n"
    return httpx.Response(
        status_code=status,
        headers={"content-type": "text/event-stream"},
        content=body.encode("utf-8"),
    )


@pytest.fixture
def manager():
    return MCPRemoteManager(timeout=5.0)


def _install_mock_transport(manager: MCPRemoteManager, handler):
    """Force every client the manager creates to use a MockTransport."""
    transport = httpx.MockTransport(handler)
    orig = manager._client_for

    def _patched(session_id, server_name, url, headers):
        key = (session_id, server_name)
        if key not in manager._clients:
            manager._clients[key] = httpx.AsyncClient(
                base_url=url,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    **(headers or {}),
                },
                transport=transport,
            )
        return manager._clients[key]

    manager._client_for = _patched  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_happy_path(manager):
    seen_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(json.loads(request.content))
        return _json_response({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"tools": [{"name": "create_pr", "description": "Open a PR"}]},
        })

    _install_mock_transport(manager, handler)
    result = await manager.list_tools(
        session_id="sess-1",
        server_name="gh-copilot",
        url="https://example/mcp",
        headers={"Authorization": "Bearer tok-1"},
    )

    assert "tools" in result
    assert result["tools"][0]["name"] == "create_pr"
    assert seen_requests[0]["method"] == "tools/list"
    # Monotonic id increments per (session, server).
    assert seen_requests[0]["id"] == 1


@pytest.mark.asyncio
async def test_list_tools_handles_sse_response(manager):
    """A streamable HTTP server may answer with text/event-stream;
    we read the first ``data:`` frame and treat it as the result."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response({
            "jsonrpc": "2.0", "id": 1,
            "result": {"tools": [{"name": "issue_list"}]},
        })

    _install_mock_transport(manager, handler)
    result = await manager.list_tools(
        session_id="sess-1", server_name="linear",
        url="https://example/mcp",
    )
    assert result["tools"][0]["name"] == "issue_list"


@pytest.mark.asyncio
async def test_list_tools_surfaces_http_error(manager):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=401, headers={}, content=b"unauthorized")

    _install_mock_transport(manager, handler)
    result = await manager.list_tools(
        session_id="sess-1", server_name="gh",
        url="https://example/mcp",
        headers={"Authorization": "Bearer bad"},
    )
    assert "error" in result
    assert "HTTP 401" in result["error"]


@pytest.mark.asyncio
async def test_list_tools_surfaces_jsonrpc_error(manager):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        })

    _install_mock_transport(manager, handler)
    result = await manager.list_tools(
        session_id="sess-1", server_name="x",
        url="https://example/mcp",
    )
    assert "error" in result
    assert "Method not found" in result["error"]


# ---------------------------------------------------------------------------
# invoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_passes_tool_name_and_arguments(manager):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return _json_response({
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"content": [{"type": "text", "text": "ok"}]},
        })

    _install_mock_transport(manager, handler)
    result = await manager.invoke(
        session_id="sess-1", server_name="gh-copilot",
        url="https://example/mcp",
        tool_name="create_pr",
        arguments={"title": "Hello", "body": "world"},
        headers={"Authorization": "Bearer tok-2"},
    )

    assert "result" in result
    body = seen[0]
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "create_pr"
    assert body["params"]["arguments"] == {"title": "Hello", "body": "world"}


@pytest.mark.asyncio
async def test_invoke_injects_authorization_header(manager):
    seen_headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        return _json_response({"jsonrpc": "2.0", "id": 1, "result": {}})

    _install_mock_transport(manager, handler)
    await manager.invoke(
        session_id="sess-1", server_name="gh",
        url="https://example/mcp",
        tool_name="x",
        headers={"Authorization": "Bearer secret-tok"},
    )

    assert seen_headers[0].get("authorization") == "Bearer secret-tok"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_server_closes_client(manager):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"jsonrpc": "2.0", "id": 1, "result": {}})

    _install_mock_transport(manager, handler)
    await manager.list_tools("sess-1", "gh", "https://example/mcp")
    assert ("sess-1", "gh") in manager._clients

    await manager.stop_server("sess-1", "gh")
    assert ("sess-1", "gh") not in manager._clients


@pytest.mark.asyncio
async def test_stop_all_closes_every_client_for_session(manager):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"jsonrpc": "2.0", "id": 1, "result": {}})

    _install_mock_transport(manager, handler)
    await manager.list_tools("sess-1", "gh", "https://example/mcp")
    await manager.list_tools("sess-1", "linear", "https://example/mcp")
    await manager.list_tools("sess-2", "gh", "https://example/mcp")

    await manager.stop_all("sess-1")
    assert ("sess-1", "gh") not in manager._clients
    assert ("sess-1", "linear") not in manager._clients
    # Other session untouched.
    assert ("sess-2", "gh") in manager._clients


@pytest.mark.asyncio
async def test_request_id_increments_per_session_server(manager):
    seen_ids: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_ids.append(body["id"])
        return _json_response({"jsonrpc": "2.0", "id": body["id"], "result": {}})

    _install_mock_transport(manager, handler)
    await manager.list_tools("sess-1", "gh", "https://example/mcp")
    await manager.list_tools("sess-1", "gh", "https://example/mcp")
    await manager.invoke("sess-1", "gh", "https://example/mcp", "x")
    assert seen_ids == [1, 2, 3]
