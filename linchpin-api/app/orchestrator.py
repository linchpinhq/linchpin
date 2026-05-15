"""Orchestrator — one async task per session driving the agent loop.

Constructs conversation context from the event log, sends to the model
provider, processes response content blocks (text, tool_use, thinking),
evaluates tool permissions via PolicyEvaluator, and manages session state
transitions.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 6.2, 6.3, 6.4, 6.7, 16.2, 16.4, 17.1, 17.3, 23.1, 23.2, 23.3, 23.4, 6.8
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Literal

import httpx

from app.db import execute, fetch_all, fetch_one, listen, notify
from app.events import append_event
from app.models import Agent, BuiltinToolConfig, CustomToolConfig, MCPServerConfig, ModelConfig
from app.policy import PolicyEvaluator
from app.providers import ContentBlock, ModelResponse, ProviderError, StreamChunk, get_provider
from app.sandbox import DockerSandbox
from app.tools import execute_builtin_tool
from app.credentials import CredentialResolver
from app.streaming import start_stream, append_delta, finish_stream

logger = logging.getLogger("linchpin-api.orchestrator")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def load_session(session_id: str) -> dict[str, Any]:
    """Fetch a session row from the database."""
    uid = uuid.UUID(session_id)
    row = await fetch_one("SELECT * FROM sessions WHERE id = $1", uid)
    if row is None:
        raise ValueError(f"Session {session_id} not found")
    result: dict[str, Any] = dict(row)
    # Parse vault_ids from JSONB — may be a JSON string or already a list
    vault_ids_raw = result.get("vault_ids")
    if vault_ids_raw is None:
        result["vault_ids"] = []
    elif isinstance(vault_ids_raw, str):
        result["vault_ids"] = json.loads(vault_ids_raw)
    elif isinstance(vault_ids_raw, list):
        result["vault_ids"] = vault_ids_raw
    else:
        result["vault_ids"] = []
    return result


async def load_agent(agent_id: str) -> Agent:
    """Fetch an agent row from the database and parse into an Agent model."""
    uid = uuid.UUID(agent_id)
    row = await fetch_one("SELECT * FROM agents WHERE id = $1", uid)
    if row is None:
        raise ValueError(f"Agent {agent_id} not found")

    tools_raw = row["tools"]
    mcp_raw = row["mcp_servers"]
    model_raw = row["model"]

    tools = json.loads(tools_raw) if isinstance(tools_raw, str) else tools_raw
    mcp_servers = json.loads(mcp_raw) if isinstance(mcp_raw, str) else mcp_raw
    model = json.loads(model_raw) if isinstance(model_raw, str) else model_raw

    return Agent(
        id=str(row["id"]),
        name=row["name"],
        version=row["version"],
        model=ModelConfig(**model),
        system=row["system"],
        tools=tools,
        mcp_servers=mcp_servers,
        created_at=row["created_at"],
    )



async def transition(session_id: str, new_status: str) -> None:
    """Update a session's status in the database."""
    uid = uuid.UUID(session_id)
    await fetch_one(
        "UPDATE sessions SET status = $1, updated_at = now() WHERE id = $2 RETURNING id",
        new_status,
        uid,
    )


async def update_usage(session_id: str, usage: dict) -> None:
    """Increment session token usage counters (v0.2.0 item #10).

    Accumulates four counters: prompt input/output and prompt-caching
    create/read. Providers that don't report cache metrics (Ollama, the
    OpenAI Chat Completions shape) leave those at 0, which is correct.

    Defensive cast: existing v0.1 rows have a usage JSONB with only
    `input_tokens` + `output_tokens` keys. ``COALESCE((usage->>'k')::int, 0)``
    handles the missing-key case so the UPDATE doesn't error on rows
    created before this migration. New sessions ship all four keys
    pre-seeded (see ``routes/sessions.py:create_session``).
    """
    uid = uuid.UUID(session_id)
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_creation_input_tokens = usage.get("cache_creation_input_tokens", 0)
    cache_read_input_tokens = usage.get("cache_read_input_tokens", 0)
    await fetch_one(
        """
        UPDATE sessions
        SET usage = jsonb_set(
                jsonb_set(
                    jsonb_set(
                        jsonb_set(usage, '{input_tokens}',
                            to_jsonb(COALESCE((usage->>'input_tokens')::int, 0) + $1)),
                        '{output_tokens}',
                        to_jsonb(COALESCE((usage->>'output_tokens')::int, 0) + $2)),
                    '{cache_creation_input_tokens}',
                    to_jsonb(COALESCE((usage->>'cache_creation_input_tokens')::int, 0) + $3)),
                '{cache_read_input_tokens}',
                to_jsonb(COALESCE((usage->>'cache_read_input_tokens')::int, 0) + $4)),
            updated_at = now()
        WHERE id = $5
        RETURNING id
        """,
        input_tokens,
        output_tokens,
        cache_creation_input_tokens,
        cache_read_input_tokens,
        uid,
    )


# ---------------------------------------------------------------------------
# Connector helpers
# ---------------------------------------------------------------------------

# Tool type classification result
ToolKind = Literal["builtin", "mcp", "custom_http"]


def get_connector_url() -> str:
    """Return the connector base URL from env var (default: http://linchpin-connector:8001)."""
    return os.environ.get("CONNECTOR_URL", "http://linchpin-connector:8001")


def classify_tool(tool_name: str, agent: Agent) -> ToolKind:
    """Determine whether a tool is built-in, MCP, or custom_http.

    Checks the agent's tool and mcp_servers config to classify the tool.
    - If the tool name matches an MCP server tool pattern (server_name__tool), it's MCP.
    - If the tool matches a CustomToolConfig entry, it's custom_http.
    - Otherwise, it's built-in.
    """
    # Check MCP servers — MCP tools are named as "server_name__tool_name"
    for mcp_server in agent.mcp_servers:
        if tool_name.startswith(f"{mcp_server.name}__"):
            return "mcp"

    # Check custom tools
    for tool_cfg in agent.tools:
        if isinstance(tool_cfg, CustomToolConfig) and tool_cfg.name == tool_name:
            return "custom_http"

    return "builtin"


def _find_mcp_server_for_tool(tool_name: str, agent: Agent) -> MCPServerConfig | None:
    """Find the MCP server config for a tool named 'server__tool'."""
    for mcp_server in agent.mcp_servers:
        if tool_name.startswith(f"{mcp_server.name}__"):
            return mcp_server
    return None


def _find_custom_tool_config(tool_name: str, agent: Agent) -> CustomToolConfig | None:
    """Find the CustomToolConfig for a given tool name."""
    for tool_cfg in agent.tools:
        if isinstance(tool_cfg, CustomToolConfig) and tool_cfg.name == tool_name:
            return tool_cfg
    return None


async def invoke_connector(
    session_id: str,
    tool_type: ToolKind,
    tool_name: str,
    arguments: dict,
    server_name: str | None = None,
    credentials: dict | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    """Forward a tool call to the linchpin-connector via POST /tools/invoke.

    Returns the parsed JSON response from the connector.
    """
    url = f"{get_connector_url()}/tools/invoke"
    payload = {
        "session_id": session_id,
        "tool_type": tool_type,
        "tool_name": tool_name,
        "arguments": arguments,
    }
    if server_name is not None:
        payload["server_name"] = server_name
    if credentials is not None:
        payload["credentials"] = credentials
    if endpoint is not None:
        payload["endpoint"] = endpoint

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"Connector returned {exc.response.status_code}: {exc.response.text}", "status": "error"}
    except httpx.HTTPError as exc:
        return {"error": f"Connector request failed: {exc}", "status": "error"}


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


async def build_context(session_id: str, agent: Agent) -> list[dict]:
    """Build the conversation messages list from the session event log.

    Reads all events for the session and converts them into the messages
    format expected by model providers:
    - system message from agent config
    - user.message → user role
    - agent.message → assistant role (text)
    - agent.thinking → assistant role (thinking block, skipped for context)
    - agent.tool_use → assistant role (tool_use block)
    - agent.tool_result → tool result
    """
    uid = uuid.UUID(session_id)
    rows = await fetch_all(
        "SELECT * FROM events WHERE session_id = $1 ORDER BY seq ASC",
        uid,
    )

    messages: list[dict] = []

    # System message first
    if agent.system:
        messages.append({"role": "system", "content": agent.system})

    for row in rows:
        event_type = row["type"]
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)

        if event_type == "user.message":
            messages.append({"role": "user", "content": payload.get("content", "")})

        elif event_type == "agent.message":
            messages.append({"role": "assistant", "content": payload.get("content", "")})

        elif event_type == "agent.thinking":
            # Include thinking as a separate block for providers that support it
            pass  # Thinking is informational; not included in context replay

        elif event_type == "agent.tool_use":
            messages.append({
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": payload.get("tool_use_id", ""),
                        "name": payload.get("name", ""),
                        "input": payload.get("input", {}),
                    }
                ],
            })

        elif event_type == "agent.tool_result":
            tool_use_id = payload.get("tool_use_id", "")
            if "error" in payload:
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "is_error": True,
                            "content": payload["error"],
                        }
                    ],
                })
            else:
                result = payload.get("result", "")
                if isinstance(result, dict):
                    result = json.dumps(result)
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": str(result),
                        }
                    ],
                })

        elif event_type in ("agent.mcp_tool_use", "agent.custom_tool_use"):
            # MCP and custom tool_use events map to assistant tool_use blocks
            messages.append({
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": payload.get("tool_use_id", ""),
                        "name": payload.get("name", ""),
                        "input": payload.get("input", {}),
                    }
                ],
            })

        elif event_type == "agent.mcp_tool_result":
            # MCP tool results map to user tool_result blocks
            tool_use_id = payload.get("tool_use_id", "")
            if "error" in payload:
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "is_error": True,
                            "content": payload["error"],
                        }
                    ],
                })
            else:
                result = payload.get("result", "")
                if isinstance(result, dict):
                    result = json.dumps(result)
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": str(result),
                        }
                    ],
                })

    return messages


# ---------------------------------------------------------------------------
# Tool definitions builder
# ---------------------------------------------------------------------------


def build_tool_definitions(agent: Agent) -> list[dict]:
    """Build the tool definitions list from the agent's tool config.

    Returns a list of dicts suitable for passing to model providers.
    Includes built-in tools, custom tools, and MCP tools (discovered via connector).
    """
    tools: list[dict] = []
    for tool in agent.tools:
        if hasattr(tool, "configs"):
            # BuiltinToolConfig
            for cfg in tool.configs:
                if cfg.enabled:
                    tools.append({
                        "name": cfg.name,
                        "description": f"Built-in tool: {cfg.name}",
                        "input_schema": {"type": "object", "properties": {}},
                    })
        elif hasattr(tool, "name"):
            # CustomToolConfig
            tools.append({
                "name": tool.name,
                "description": getattr(tool, "description", ""),
                "input_schema": getattr(tool, "input_schema", {"type": "object", "properties": {}}),
            })
    return tools


async def build_tool_definitions_with_mcp(
    agent: Agent, session_id: str, vault_ids: list[str] | None = None,
) -> list[dict]:
    """Build tool definitions including MCP tools discovered from the connector.

    Calls the connector's POST /tools/list for each MCP server to discover
    available tools, then prefixes them with server_name__.
    Resolves vault credentials and passes them as env vars to the MCP server.
    """
    # Start with built-in and custom tools
    tools = build_tool_definitions(agent)

    # Discover MCP tools from each configured server
    if agent.mcp_servers:
        connector_url = get_connector_url()
        for mcp_server in agent.mcp_servers:
            try:
                # Merge agent-configured env with vault credentials
                env = dict(mcp_server.env)
                if vault_ids:
                    mcp_url = env.get("MCP_SERVER_URL") or mcp_server.command or mcp_server.name
                    resolver = CredentialResolver()
                    vault_creds = await resolver.resolve_mcp_credential(vault_ids, mcp_url)
                    if vault_creds:
                        auth_type = vault_creds.get("auth_type")
                        if auth_type == "bearer_token":
                            env["AUTHORIZATION"] = f"Bearer {vault_creds.get('token', '')}"
                        elif auth_type == "oauth":
                            env["AUTHORIZATION"] = f"Bearer {vault_creds.get('access_token', '')}"

                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        f"{connector_url}/tools/list",
                        json={
                            "session_id": session_id,
                            "server_name": mcp_server.name,
                            "command": mcp_server.command or "npx",
                            "args": mcp_server.args,
                            "env": env,
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("tools"):
                            for mcp_tool in data["tools"]:
                                tool_name = mcp_tool.get("name", "")
                                tools.append({
                                    "name": f"{mcp_server.name}__{tool_name}",
                                    "description": mcp_tool.get("description", ""),
                                    "input_schema": mcp_tool.get("inputSchema", mcp_tool.get("input_schema", {"type": "object", "properties": {}})),
                                })
                            logger.info(
                                "Discovered %d tools from MCP server '%s' for session %s",
                                len(data["tools"]), mcp_server.name, session_id,
                            )
                        elif data.get("error"):
                            logger.warning(
                                "MCP tools/list error for '%s': %s",
                                mcp_server.name, data["error"],
                            )
            except Exception as exc:
                logger.warning(
                    "Failed to discover MCP tools from '%s' for session %s: %s",
                    mcp_server.name, session_id, exc,
                )

    return tools


# ---------------------------------------------------------------------------
# Confirmation waiter
# ---------------------------------------------------------------------------


async def wait_for_confirmation(session_id: str, tool_use_id: str) -> dict:
    """Block on LISTEN/NOTIFY until a user.tool_confirmation event arrives.

    Returns the confirmation payload dict.
    """
    channel = f"session_{session_id.replace('-', '_')}"
    uid = uuid.UUID(session_id)

    async for _payload in listen(channel):
        # Check for a tool_confirmation event matching our tool_use_id
        rows = await fetch_all(
            """
            SELECT * FROM events
            WHERE session_id = $1 AND type = 'user.tool_confirmation'
            ORDER BY seq DESC LIMIT 10
            """,
            uid,
        )
        for row in rows:
            ev_payload = row["payload"]
            if isinstance(ev_payload, str):
                ev_payload = json.loads(ev_payload)
            if ev_payload.get("tool_use_id") == tool_use_id:
                return ev_payload

    # Should not reach here in normal operation
    return {"result": "deny", "deny_message": "No confirmation received"}


async def wait_for_custom_tool_result(session_id: str, tool_use_id: str) -> dict:
    """Block on LISTEN/NOTIFY until a user.custom_tool_result event arrives.

    Returns the result payload dict.
    """
    channel = f"session_{session_id.replace('-', '_')}"
    uid = uuid.UUID(session_id)

    async for _payload in listen(channel):
        rows = await fetch_all(
            """
            SELECT * FROM events
            WHERE session_id = $1 AND type = 'user.custom_tool_result'
            ORDER BY seq DESC LIMIT 10
            """,
            uid,
        )
        for row in rows:
            ev_payload = row["payload"]
            if isinstance(ev_payload, str):
                ev_payload = json.loads(ev_payload)
            if ev_payload.get("tool_use_id") == tool_use_id:
                return ev_payload

    return {"result": "", "error": "No custom tool result received"}


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------


async def dispatch_tool(
    session_id: str,
    block: ContentBlock,
    sandbox: DockerSandbox,
    agent: Agent,
    vault_ids: list[str] | None = None,
) -> tuple[str, str]:
    """Route a tool call to the appropriate handler based on tool type.

    Returns a tuple of (result_json, event_type) where event_type is one of:
    - "agent.tool_result" for built-in tools
    - "agent.mcp_tool_result" for MCP tools
    - "agent.tool_result" for custom_http tools (result comes back directly)
    """
    tool_name = block.tool_name or ""
    tool_input = block.tool_input or {}

    try:
        kind = classify_tool(tool_name, agent)

        if kind == "mcp":
            mcp_server = _find_mcp_server_for_tool(tool_name, agent)
            server_name = mcp_server.name if mcp_server else None
            credentials = mcp_server.env if mcp_server else None
            # Strip the server prefix to get the actual tool name for the MCP server
            actual_tool_name = tool_name
            if server_name and tool_name.startswith(f"{server_name}__"):
                actual_tool_name = tool_name[len(server_name) + 2:]

            # Resolve MCP credentials from vaults if available
            if vault_ids and mcp_server:
                # Use the MCP server command as a URL-like identifier for credential lookup
                mcp_url = mcp_server.env.get("MCP_SERVER_URL") or mcp_server.command
                resolver = CredentialResolver()
                vault_creds = await resolver.resolve_mcp_credential(vault_ids, mcp_url)
                if vault_creds is not None:
                    credentials = vault_creds

            connector_resp = await invoke_connector(
                session_id=session_id,
                tool_type="mcp",
                tool_name=actual_tool_name,
                arguments=tool_input,
                server_name=server_name,
                credentials=credentials,
            )
            if connector_resp.get("status") == "error":
                return json.dumps({"error": connector_resp.get("error", "MCP tool invocation failed")}), "agent.mcp_tool_result"
            return json.dumps(connector_resp.get("result", {})), "agent.mcp_tool_result"

        elif kind == "custom_http":
            custom_cfg = _find_custom_tool_config(tool_name, agent)
            endpoint = getattr(custom_cfg, "endpoint", None) if custom_cfg else None

            if endpoint:
                # Tool has an endpoint — forward to connector for direct invocation
                connector_resp = await invoke_connector(
                    session_id=session_id,
                    tool_type="custom_http",
                    tool_name=tool_name,
                    arguments=tool_input,
                    endpoint=endpoint,
                )
                if connector_resp.get("status") == "error":
                    return json.dumps({"error": connector_resp.get("error", "Custom tool invocation failed")}), "agent.tool_result"
                return json.dumps(connector_resp.get("result", {})), "agent.tool_result"
            else:
                # No endpoint — this is a client-handled custom tool.
                # The orchestrator emits agent.custom_tool_use and waits for
                # user.custom_tool_result. Return a sentinel so the caller
                # knows to handle the async flow.
                return "__custom_tool_needs_client__", "agent.custom_tool_use"

        else:
            # Built-in tool
            session = await load_session(session_id)
            container_id = session.get("container_id")
            if not container_id:
                return json.dumps({"error": "No container associated with session"}), "agent.tool_result"

            result = await execute_builtin_tool(tool_name, tool_input, sandbox, container_id)
            return json.dumps(result), "agent.tool_result"

    except Exception as exc:
        return json.dumps({"error": str(exc)}), "agent.tool_result"


# ---------------------------------------------------------------------------
# Main orchestrator loop
# ---------------------------------------------------------------------------


async def check_for_interrupt(session_id: str) -> bool:
    """Check if the most recent event for this session is a user.interrupt.

    Returns True if an interrupt was detected.
    """
    uid = uuid.UUID(session_id)
    row = await fetch_one(
        """
        SELECT type FROM events
        WHERE session_id = $1
        ORDER BY seq DESC LIMIT 1
        """,
        uid,
    )
    return row is not None and row["type"] == "user.interrupt"


async def handle_interrupt(session_id: str) -> None:
    """Transition session to idle with stop_reason='interrupt'."""
    await transition(session_id, "idle")
    await append_event(session_id, "session.status_idle", {
        "stop_reason": "interrupt",
    })


async def run_session(session_id: str, sandbox: DockerSandbox) -> None:
    """Main orchestrator loop for a single session.

    Runs as an async task. Loops:
    1. Load session, exit if terminal state
    2. Wait for input via LISTEN/NOTIFY
    3. Check for user.interrupt — if so, transition to idle
    4. Build context from event log
    5. Send to model provider
    6. Process response blocks (thinking, text, tool_use)
    7. Between tool calls, check for interrupts
    8. If end_turn, transition to idle; if tool calls, loop back
    """
    logger.info("Orchestrator started for session %s", session_id)

    while True:
        try:
            # 1. Load session from DB
            session = await load_session(session_id)
            status = session["status"]

            if status in ("terminated", "failed"):
                logger.info("Session %s is %s, orchestrator exiting.", session_id, status)
                return

            # 2. Wait for input event via LISTEN/NOTIFY
            channel = f"session_{session_id.replace('-', '_')}"
            async for _payload in listen(channel):
                break  # Got a notification, proceed

            # Re-check status after waking up
            session = await load_session(session_id)
            status = session["status"]
            if status in ("terminated", "failed"):
                logger.info("Session %s is %s after wake, orchestrator exiting.", session_id, status)
                return

            # 3. Check for user.interrupt before processing
            if await check_for_interrupt(session_id):
                logger.info("Session %s interrupted by user.", session_id)
                await handle_interrupt(session_id)
                continue

            # Transition to running
            await transition(session_id, "running")

            # 4. Load agent config from DB
            agent = await load_agent(str(session["agent_id"]))

            # 5. Build conversation context from event log
            messages = await build_context(session_id, agent)

            # 6. Resolve API key from vaults (if session has vault_ids).
            # Sessions without vault_ids fall through to the env-var lookup
            # below (and providers that don't need a key, like Ollama, may
            # leave it as None).
            env_var_map = {
                "openrouter": "OPENROUTER_API_KEY",
            }
            env_var_name = env_var_map.get(agent.model.provider)
            env_key = os.environ.get(env_var_name, "") if env_var_name else ""

            resolved_api_key: str | None = None
            vault_ids = session.get("vault_ids") or []
            if vault_ids:
                resolver = CredentialResolver()
                resolved_api_key = await resolver.resolve_api_key(vault_ids, agent.model.provider)

                # Vault-bound session but no credential found — try env fallback.
                if resolved_api_key is None:
                    if env_key:
                        resolved_api_key = env_key
                    elif agent.model.provider != "ollama":
                        await append_event(session_id, "session.error", {
                            "error": f"No API key found for provider '{agent.model.provider}'. "
                                     f"Configure a vault credential or set the "
                                     f"{env_var_name or agent.model.provider.upper() + '_API_KEY'} environment variable.",
                            "source": "credential_resolution",
                        })
                        await transition(session_id, "failed")
                        return
            else:
                # No vault bound — fall back to env var so the provider has auth.
                resolved_api_key = env_key or None

            # 7. Get model provider and stream
            provider = get_provider(agent.model)
            tools = await build_tool_definitions_with_mcp(agent, session_id, vault_ids=vault_ids)

            # --- Streaming integration ---
            message_id = str(uuid.uuid4())
            accumulated_text = ""
            content_blocks: list[ContentBlock] = []
            stop_reason: str | None = None
            usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
            deltas_emitted = False

            # Start in-memory stream for live delta delivery (no DB writes for deltas)
            await start_stream(session_id, message_id)

            # v0.2.0 item #9 — span around the model request. We emit
            # `span.model_request_start` before the streaming call begins and
            # pair it with `span.model_request_end` after the stream
            # completes (success OR failure). `span_id` lets clients pair the
            # two events; `elapsed_ms` is filled in on end. Both emissions
            # are best-effort — span failures must not abort the agent loop.
            span_id = str(uuid.uuid4())
            span_started_at = time.monotonic()
            try:
                await append_event(session_id, "span.model_request_start", {
                    "span_id": span_id,
                    "model": {
                        "provider": agent.model.provider,
                        "id": agent.model.id,
                    },
                })
            except Exception:
                logger.exception("failed to append span.model_request_start")

            span_error: str | None = None
            try:
                async for chunk in provider.send_streaming(
                    messages, agent.model, tools or None, api_key=resolved_api_key,
                ):
                    if chunk.type == "text_delta":
                        accumulated_text += chunk.text or ""
                        # Write to in-memory buffer only — no DB write
                        await append_delta(session_id, chunk.text or "")
                        deltas_emitted = True
                    elif chunk.type == "thinking":
                        await append_event(session_id, "agent.thinking", {"text": chunk.text or ""})
                    elif chunk.type == "tool_use":
                        content_blocks.append(ContentBlock(
                            type="tool_use",
                            tool_use_id=chunk.tool_use_id,
                            tool_name=chunk.tool_name,
                            tool_input=chunk.tool_input,
                        ))
                    elif chunk.type == "final":
                        stop_reason = chunk.stop_reason
                        usage = chunk.usage or usage
            except Exception as stream_exc:
                span_error = str(stream_exc)
                # Emit the span end on failure too so observability tools see
                # paired start/end events for every request. The full exception
                # is logged + surfaced via session.error below; we don't
                # re-raise from the span emit.
                try:
                    await append_event(session_id, "span.model_request_end", {
                        "span_id": span_id,
                        "model": {
                            "provider": agent.model.provider,
                            "id": agent.model.id,
                        },
                        "model_usage": usage,
                        "elapsed_ms": int((time.monotonic() - span_started_at) * 1000),
                        "error": span_error,
                    })
                except Exception:
                    logger.exception("failed to append span.model_request_end on failure")
                # Clean up the in-memory stream
                asyncio.create_task(finish_stream(session_id))
                if deltas_emitted:
                    logger.error("Mid-stream error in session %s: %s", session_id, stream_exc)
                    await append_event(session_id, "session.error", {
                        "error": str(stream_exc),
                        "source": "streaming",
                    })
                    await transition(session_id, "failed")
                    return
                else:
                    raise

            # Mark stream as done (cleanup happens after delay)
            asyncio.create_task(finish_stream(session_id))

            # v0.2.0 item #9 — span end on the success path, paired with the
            # start emit above by span_id. model_usage is the per-request
            # usage from the final chunk (not the cumulative session total —
            # that's tracked separately via update_usage).
            try:
                await append_event(session_id, "span.model_request_end", {
                    "span_id": span_id,
                    "model": {
                        "provider": agent.model.provider,
                        "id": agent.model.id,
                    },
                    "model_usage": usage,
                    "elapsed_ms": int((time.monotonic() - span_started_at) * 1000),
                })
            except Exception:
                logger.exception("failed to append span.model_request_end")

            # 8. Update usage stats
            await update_usage(session_id, usage)

            # 9. Emit final agent.message with full accumulated content
            if accumulated_text:
                await append_event(session_id, "agent.message", {"content": accumulated_text})

            # 10. Process tool_use content blocks
            has_tool_calls = len(content_blocks) > 0
            interrupted = False
            for block in content_blocks:
                # Check for interrupt between content blocks
                if await check_for_interrupt(session_id):
                    logger.info("Session %s interrupted during tool execution.", session_id)
                    await handle_interrupt(session_id)
                    interrupted = True
                    break

                # Classify the tool to determine the correct event types
                tool_kind = classify_tool(block.tool_name or "", agent)

                # Emit the appropriate tool_use event
                if tool_kind == "mcp":
                    use_event_type = "agent.mcp_tool_use"
                elif tool_kind == "custom_http":
                    use_event_type = "agent.custom_tool_use"
                else:
                    use_event_type = "agent.tool_use"

                await append_event(session_id, use_event_type, {
                    "tool_use_id": block.tool_use_id or "",
                    "name": block.tool_name or "",
                    "input": block.tool_input or {},
                })

                # Evaluate policy
                evaluator = PolicyEvaluator(agent)
                permission = evaluator.evaluate(block.tool_name or "")

                if permission == "denied":
                    await append_event(session_id, "agent.tool_result", {
                        "tool_use_id": block.tool_use_id or "",
                        "error": f"Tool '{block.tool_name}' is not available",
                    })
                    continue

                if permission == "always_ask":
                    # Emit requires_action, wait for confirmation
                    await append_event(session_id, "session.requires_action", {
                        "tool_use_id": block.tool_use_id or "",
                        "tool_name": block.tool_name or "",
                    })
                    # Wait for user.tool_confirmation event
                    confirmation = await wait_for_confirmation(
                        session_id, block.tool_use_id or ""
                    )
                    if confirmation.get("result") == "deny":
                        await append_event(session_id, "agent.tool_result", {
                            "tool_use_id": block.tool_use_id or "",
                            "error": confirmation.get(
                                "deny_message", "Tool call denied by user"
                            ),
                        })
                        continue

                # Execute the tool (always_allow or confirmed)
                result, result_event_type = await dispatch_tool(
                    session_id, block, sandbox, agent, vault_ids=vault_ids,
                )

                if result == "__custom_tool_needs_client__":
                    # Client-handled custom tool: wait for user.custom_tool_result
                    custom_result = await wait_for_custom_tool_result(
                        session_id, block.tool_use_id or ""
                    )
                    await append_event(session_id, "agent.tool_result", {
                        "tool_use_id": block.tool_use_id or "",
                        "result": custom_result.get("result", ""),
                    })
                else:
                    await append_event(session_id, result_event_type, {
                        "tool_use_id": block.tool_use_id or "",
                        "result": result,
                    })

            if interrupted:
                continue

            # 11. If no tool calls (end_turn), transition to idle
            if stop_reason == "end_turn" or not has_tool_calls:
                await transition(session_id, "idle")
                await append_event(session_id, "session.status_idle", {
                    "stop_reason": stop_reason or "end_turn",
                })
            # If tool calls were made, loop back (model will see tool results)

        except ProviderError as exc:
            logger.error("Provider error in session %s: %s", session_id, exc)
            await append_event(session_id, "session.error", {
                "error": str(exc),
                "source": "model_provider",
            })
            await transition(session_id, "failed")
            return

        except Exception as exc:
            logger.exception("Unrecoverable error in session %s", session_id)
            try:
                await append_event(session_id, "session.error", {
                    "error": str(exc),
                    "source": "orchestrator",
                })
                await transition(session_id, "failed")
            except Exception:
                logger.exception("Failed to record error for session %s", session_id)
            return


# ---------------------------------------------------------------------------
# Session recovery on startup
# ---------------------------------------------------------------------------


async def recover_sessions(
    sandbox: DockerSandbox,
    orchestrator_tasks: dict[str, Any],
    *,
    watcher_tasks: dict[str, Any] | None = None,
    spawn_watcher: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Recover non-terminal sessions after process restart.

    1. Query for sessions with status in (running, idle, rescheduling).
    2. Set each to ``rescheduling``, emit ``session.status_rescheduled``.
    3. Check if the container still exists.
    4. If container alive → transition to ``idle``, start orchestrator task,
       and (PR5 D4) re-spawn the deliverables watcher so its boot scan
       ingests anything written during the outage.
    5. If container missing → transition to ``failed``, emit ``session.error``.

    ``spawn_watcher`` is passed in (not imported) to avoid a circular
    import between orchestrator and watcher; ``app.main.lifespan`` wires it
    to ``watch_session_deliverables``. When omitted (legacy callers, unit
    tests), watcher re-spawn is skipped without erroring.

    Requirements: 23.1, 23.2, 23.3, 23.4, 6.8
    """
    rows = await fetch_all(
        "SELECT * FROM sessions WHERE status IN ('running', 'idle', 'rescheduling')"
    )

    for row in rows:
        session_id = str(row["id"])
        container_id = row.get("container_id")

        try:
            # Mark as rescheduling
            await transition(session_id, "rescheduling")
            await append_event(session_id, "session.status_rescheduled", {
                "reason": "process_restart",
            })

            # Check if container still exists
            if container_id:
                try:
                    await sandbox.exec(container_id, "true")
                    # Container is alive — transition to idle and start orchestrator
                    await transition(session_id, "idle")
                    task = asyncio.create_task(run_session(session_id, sandbox))
                    orchestrator_tasks[session_id] = task

                    # PR5 D4 — re-attach the deliverables watcher. Its boot
                    # scan picks up files written to the outputs bind while
                    # the api was down; the awatch loop then takes over.
                    if spawn_watcher is not None and watcher_tasks is not None:
                        watcher_task = asyncio.create_task(spawn_watcher(session_id))

                        def _watcher_done(t: asyncio.Task, _sid: str = session_id) -> None:
                            if t.cancelled():
                                return
                            exc = t.exception()
                            if exc:
                                logger.error(
                                    "Recovered watcher for session %s failed: %s", _sid, exc,
                                )

                        watcher_task.add_done_callback(_watcher_done)
                        watcher_tasks[session_id] = watcher_task
                except Exception:
                    # Container is gone
                    await transition(session_id, "failed")
                    await append_event(session_id, "session.error", {
                        "error": "Container not found after process restart",
                        "source": "recovery",
                    })
            else:
                # No container_id at all
                await transition(session_id, "failed")
                await append_event(session_id, "session.error", {
                    "error": "Container not found after process restart",
                    "source": "recovery",
                })

        except Exception:
            logger.exception("Failed to recover session %s", session_id)
            try:
                await transition(session_id, "failed")
                await append_event(session_id, "session.error", {
                    "error": "Recovery failed due to unexpected error",
                    "source": "recovery",
                })
            except Exception:
                logger.exception("Failed to mark session %s as failed", session_id)


# ---------------------------------------------------------------------------
# Session TTL cleanup background task
# ---------------------------------------------------------------------------


async def cleanup_expired_sessions(
    sandbox: DockerSandbox,
    orchestrator_tasks: dict[str, Any],
    interval_seconds: int = 60,
) -> None:
    """Background task that periodically checks for and terminates expired sessions.

    A session is expired when:
    - ttl_seconds IS NOT NULL
    - status NOT IN ('terminated', 'failed')
    - updated_at + ttl_seconds < now()

    On expiry: set status to 'terminated', destroy container, cancel orchestrator task.

    Requirements: 19.1, 19.2
    """
    import asyncio

    logger.info("Session TTL cleanup task started (interval=%ds).", interval_seconds)

    while True:
        try:
            await asyncio.sleep(interval_seconds)

            rows = await fetch_all(
                """
                SELECT id, container_id
                FROM sessions
                WHERE ttl_seconds IS NOT NULL
                  AND status NOT IN ('terminated', 'failed')
                  AND updated_at + (ttl_seconds || ' seconds')::interval < now()
                """,
            )

            for row in rows:
                session_id = str(row["id"])
                container_id = row["container_id"]
                logger.info("Session %s expired (TTL). Terminating.", session_id)

                try:
                    # Update status to terminated
                    await transition(session_id, "terminated")
                    # Mirror terminate_session: drop any live resource mounts
                    # to 'unmounted' so DELETE /v1/files no longer 409s on
                    # files that were only mounted in TTL-expired sessions.
                    await execute(
                        """UPDATE session_resources
                           SET state = 'unmounted', unmounted_at = now()
                           WHERE session_id = $1
                             AND state IN ('mounted', 'unmounting')""",
                        uuid.UUID(session_id),
                    )
                    await append_event(session_id, "session.status_terminated", {
                        "reason": "ttl_expired",
                    })

                    # Cancel orchestrator task
                    task = orchestrator_tasks.pop(session_id, None)
                    if task is not None and not task.done():
                        task.cancel()

                    # Destroy container
                    if container_id:
                        await sandbox.destroy(container_id)

                except Exception:
                    logger.exception("Error terminating expired session %s", session_id)

        except asyncio.CancelledError:
            logger.info("Session TTL cleanup task cancelled.")
            return
        except Exception:
            logger.exception("Error in TTL cleanup loop")
