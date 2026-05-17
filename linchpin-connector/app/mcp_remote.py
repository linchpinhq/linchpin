"""Remote URL MCP transport for Linchpin Connector (v0.5.0).

The stdio transport in ``mcp.py`` spawns a subprocess. URL MCP servers
(GitHub Copilot MCP, Linear MCP, …) live behind HTTP — we talk to them
over the MCP `streamable HTTP` transport: each JSON-RPC request is a
POST whose response is either a single JSON document or a Server-Sent
Events stream. This module owns the per-(session, server) connection
state for the URL variant.

Auth is injected by the caller: the Linchpin API resolves the
``vault_ids[]`` on the URL-MCP server config to a credential value and
hands us the Bearer token via ``headers``. We don't load secrets
ourselves — the connector is intentionally credential-blind so a leak
here can't surface vault contents.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("linchpin-connector.mcp_remote")

DEFAULT_TIMEOUT = 30.0


class MCPRemoteError(Exception):
    """Raised when a remote MCP transport call fails irrecoverably."""


class MCPRemoteManager:
    """Manages per-(session, server) HTTP clients for URL MCP servers.

    One ``httpx.AsyncClient`` per pair keeps keep-alive + auth headers
    pinned. We don't persist anything across process restarts — the
    spec's MCP `streamable HTTP` transport is request-response by
    design; per-session JSON-RPC state lives client-side, not in a
    server-side session id we'd need to recover.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._clients: dict[tuple[str, str], httpx.AsyncClient] = {}
        self._request_ids: dict[tuple[str, str], int] = {}

    def _next_id(self, session_id: str, server_name: str) -> int:
        key = (session_id, server_name)
        self._request_ids[key] = self._request_ids.get(key, 0) + 1
        return self._request_ids[key]

    def _client_for(
        self,
        session_id: str,
        server_name: str,
        url: str,
        headers: dict[str, str] | None,
    ) -> httpx.AsyncClient:
        key = (session_id, server_name)
        client = self._clients.get(key)
        if client is not None:
            return client
        client = httpx.AsyncClient(
            base_url=url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **(headers or {}),
            },
            timeout=self.timeout,
        )
        self._clients[key] = client
        return client

    async def _rpc(
        self,
        *,
        session_id: str,
        server_name: str,
        url: str,
        method: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> dict[str, Any]:
        """Send one JSON-RPC request. Returns either ``{"result": ...}``
        or ``{"error": "..."}``.

        We treat a `text/event-stream` response by reading the body in
        full and picking the first ``data:`` JSON message — full SSE
        streaming is out of scope for v0.5.0 (the agent loop is
        request-response anyway).
        """
        client = self._client_for(session_id, server_name, url, headers)
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(session_id, server_name),
            "method": method,
            "params": params or {},
        }
        try:
            resp = await client.post("", json=payload)
        except httpx.HTTPError as exc:
            return {"error": f"MCP remote request failed: {exc}"}

        if resp.status_code >= 400:
            return {
                "error": (
                    f"MCP remote returned HTTP {resp.status_code} "
                    f"({resp.reason_phrase})"
                ),
            }

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            body = await self._read_first_sse_message(resp)
            if body is None:
                return {"error": "MCP remote SSE stream produced no data"}
            data = body
        else:
            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                return {"error": f"Invalid JSON from MCP remote: {exc}"}

        if "error" in data:
            return {"error": str(data["error"])}
        return {"result": data.get("result", data)}

    @staticmethod
    async def _read_first_sse_message(resp: httpx.Response) -> dict | None:
        """Pull the first ``data: { … }`` frame out of an SSE stream and
        return its parsed JSON. Anything after the first event is
        ignored (request-response is the only call shape we use here)."""
        try:
            text = (await resp.aread()).decode("utf-8", errors="replace")
        except httpx.HTTPError:
            return None
        for line in text.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
        return None

    async def list_tools(
        self,
        session_id: str,
        server_name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Discover tools at a URL MCP server. Returns ``{"tools": [...]}``."""
        out = await self._rpc(
            session_id=session_id,
            server_name=server_name,
            url=url,
            method="tools/list",
            params={},
            headers=headers,
        )
        if "error" in out:
            return out
        result = out["result"]
        if isinstance(result, dict):
            tools = result.get("tools", [])
        else:
            tools = []
        return {"tools": tools}

    async def invoke(
        self,
        session_id: str,
        server_name: str,
        url: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Invoke a tool at a URL MCP server."""
        out = await self._rpc(
            session_id=session_id,
            server_name=server_name,
            url=url,
            method="tools/call",
            params={"name": tool_name, "arguments": arguments or {}},
            headers=headers,
        )
        return out

    async def stop_server(self, session_id: str, server_name: str) -> None:
        """Close the HTTP client for one (session, server) pair.

        URL MCP is stateless on our side — we just shut the connection
        pool so we don't leak sockets after a session terminates.
        """
        key = (session_id, server_name)
        client = self._clients.pop(key, None)
        self._request_ids.pop(key, None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    async def stop_all(self, session_id: str) -> None:
        keys = [k for k in list(self._clients) if k[0] == session_id]
        for key in keys:
            await self.stop_server(*key)

    async def aclose(self) -> None:
        """Shut every client. Called on connector lifespan exit."""
        for key in list(self._clients):
            await self.stop_server(*key)
