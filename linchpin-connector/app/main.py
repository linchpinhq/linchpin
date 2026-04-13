"""Linchpin Connector — MCP server management and custom HTTP tool invocation."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel

from app.http_tools import invoke_http_tool
from app.mcp import MCPManager

logger = logging.getLogger("linchpin-connector")

app = FastAPI(
    title="Linchpin Connector",
    version="0.1.0",
    description="MCP server management and custom HTTP tool invocation",
)

# Shared MCP manager instance
mcp_manager = MCPManager()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ToolInvokeRequest(BaseModel):
    session_id: str
    tool_type: Literal["mcp", "custom_http"]
    server_name: str | None = None  # for MCP
    tool_name: str
    arguments: dict = {}
    credentials: dict | None = None  # env vars for MCP
    endpoint: str | None = None  # for custom HTTP


class ToolInvokeResponse(BaseModel):
    result: Any | None = None
    error: str | None = None
    status: str = "ok"  # "ok" or "error"


class ToolListRequest(BaseModel):
    session_id: str
    server_name: str
    command: str
    args: list[str] = []
    env: dict[str, str] = {}


class ToolListResponse(BaseModel):
    tools: list[dict] = []
    error: str | None = None


# ---------------------------------------------------------------------------
# Stub handlers (filled in by tasks 10.2 and 10.3)
# ---------------------------------------------------------------------------

async def handle_mcp_invoke(request: ToolInvokeRequest) -> ToolInvokeResponse:
    """Route an MCP tool invocation to the MCP manager."""
    if not request.server_name:
        return ToolInvokeResponse(
            error="server_name is required for MCP tool invocations",
            status="error",
        )

    # Build environment variables for the MCP server process.
    # If credentials carry auth_type-based tokens (from vault resolution),
    # inject them as environment variables for the subprocess.
    env_vars: dict[str, str] | None = None
    if request.credentials:
        auth_type = request.credentials.get("auth_type")
        if auth_type == "bearer_token":
            token = request.credentials.get("token", "")
            env_vars = {"AUTHORIZATION": f"Bearer {token}"}
        elif auth_type == "oauth":
            access_token = request.credentials.get("access_token", "")
            env_vars = {"AUTHORIZATION": f"Bearer {access_token}"}
        else:
            # Legacy format: pass credentials as-is (env vars dict)
            env_vars = {k: str(v) for k, v in request.credentials.items()}

    # Start the server if not already running
    # In a real deployment the command/args come from the agent's mcp_servers config,
    # but for the invoke path we assume the server is already started or we start it
    # from credentials/config passed in the request.
    result = await mcp_manager.invoke(
        session_id=request.session_id,
        server_name=request.server_name,
        tool_name=request.tool_name,
        arguments=request.arguments,
    )

    if "error" in result:
        return ToolInvokeResponse(
            error=result["error"],
            status="error",
        )

    return ToolInvokeResponse(result=result.get("result"), status="ok")


async def handle_http_invoke(request: ToolInvokeRequest) -> ToolInvokeResponse:
    """Route a custom HTTP tool invocation."""
    if not request.endpoint:
        return ToolInvokeResponse(
            error="endpoint is required for custom HTTP tool invocations",
            status="error",
        )

    result = await invoke_http_tool(
        endpoint=request.endpoint,
        tool_name=request.tool_name,
        arguments=request.arguments,
    )

    if "error" in result:
        return ToolInvokeResponse(
            error=result["error"],
            status="error",
        )

    return ToolInvokeResponse(result=result.get("result"), status="ok")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/tools/list", response_model=ToolListResponse)
async def list_tools(request: ToolListRequest) -> ToolListResponse:
    """Discover available tools from an MCP server.

    Spawns the MCP server subprocess if not already running,
    calls tools/list, and returns the discovered tools.
    """
    result = await mcp_manager.list_tools(
        session_id=request.session_id,
        server_name=request.server_name,
        command=request.command,
        args=request.args,
        env=request.env,
    )

    if "error" in result:
        return ToolListResponse(error=result["error"])

    return ToolListResponse(tools=result.get("tools", []))


@app.post("/tools/invoke", response_model=ToolInvokeResponse)
async def invoke_tool(request: ToolInvokeRequest) -> ToolInvokeResponse:
    """Invoke a tool via MCP or custom HTTP.

    Routes to the appropriate handler based on ``tool_type``.
    """
    if request.tool_type == "mcp":
        return await handle_mcp_invoke(request)
    else:  # custom_http
        return await handle_http_invoke(request)
