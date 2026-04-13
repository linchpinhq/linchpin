"""MCP server subprocess manager for Linchpin Connector.

Spawns MCP server subprocesses per session using stdio transport,
forwards tool calls, and handles crashes/timeouts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("linchpin-connector.mcp")

DEFAULT_TIMEOUT = 30  # seconds


class MCPManager:
    """Manages MCP server subprocesses keyed by (session_id, server_name)."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        # (session_id, server_name) -> asyncio.subprocess.Process
        self._processes: dict[tuple[str, str], asyncio.subprocess.Process] = {}

    async def start_server(
        self,
        session_id: str,
        server_name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Spawn an MCP server subprocess using stdio transport.

        Credentials are passed via environment variables.
        """
        key = (session_id, server_name)
        if key in self._processes:
            proc = self._processes[key]
            if proc.returncode is None:
                logger.info("MCP server %s already running for session %s", server_name, session_id)
                return
            # Process exited — clean up stale entry
            del self._processes[key]

        cmd = [command] + (args or [])
        import os

        merged_env = {**os.environ, **(env or {})}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        self._processes[key] = proc
        logger.info("Started MCP server %s (pid=%s) for session %s", server_name, proc.pid, session_id)

    async def list_tools(
        self,
        session_id: str,
        server_name: str,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Discover available tools from an MCP server via tools/list.

        Starts the server if not already running. Returns a dict with
        ``tools`` (list of tool defs) or ``error``.
        """
        key = (session_id, server_name)
        proc = self._processes.get(key)

        # Start server if not running
        if proc is None or proc.returncode is not None:
            if not command:
                return {"error": f"MCP server '{server_name}' not running and no command provided"}
            await self.start_server(session_id, server_name, command, args, env)
            proc = self._processes.get(key)
            if proc is None:
                return {"error": f"Failed to start MCP server '{server_name}'"}

        request_payload = json.dumps({
            "method": "tools/list",
            "params": {},
        }) + "\n"

        try:
            proc.stdin.write(request_payload.encode())  # type: ignore[union-attr]
            await proc.stdin.drain()  # type: ignore[union-attr]

            raw = await asyncio.wait_for(
                proc.stdout.readline(),  # type: ignore[union-attr]
                timeout=self.timeout,
            )

            if not raw:
                return {"error": f"MCP server '{server_name}' returned empty response"}

            response = json.loads(raw.decode())

            # MCP tools/list response format: {"tools": [{"name": ..., "description": ..., "inputSchema": ...}]}
            tools = response.get("tools", response.get("result", {}).get("tools", []))
            return {"tools": tools}

        except asyncio.TimeoutError:
            return {"error": f"MCP tools/list timed out after {self.timeout}s for '{server_name}'"}
        except json.JSONDecodeError as exc:
            return {"error": f"Invalid JSON from MCP server '{server_name}': {exc}"}
        except Exception as exc:
            return {"error": f"MCP tools/list error: {exc}"}

    async def invoke(
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a tool call to the MCP subprocess and return the result.

        Returns a dict with either ``result`` or ``error`` key.
        """
        key = (session_id, server_name)
        proc = self._processes.get(key)

        if proc is None:
            return {"error": f"MCP server '{server_name}' not found for session '{session_id}'"}

        if proc.returncode is not None:
            stderr_output = ""
            if proc.stderr:
                try:
                    stderr_output = (await proc.stderr.read()).decode(errors="replace")
                except Exception:
                    pass
            del self._processes[key]
            return {
                "error": f"MCP server '{server_name}' has crashed (exit code {proc.returncode})",
                "stderr": stderr_output,
            }

        request_payload = json.dumps({
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }) + "\n"

        try:
            proc.stdin.write(request_payload.encode())  # type: ignore[union-attr]
            await proc.stdin.drain()  # type: ignore[union-attr]

            raw = await asyncio.wait_for(
                proc.stdout.readline(),  # type: ignore[union-attr]
                timeout=self.timeout,
            )

            if not raw:
                return {"error": f"MCP server '{server_name}' returned empty response (possibly crashed)"}

            response = json.loads(raw.decode())
            return {"result": response}

        except asyncio.TimeoutError:
            return {"error": f"MCP tool call '{tool_name}' timed out after {self.timeout}s"}
        except json.JSONDecodeError as exc:
            return {"error": f"Invalid JSON from MCP server '{server_name}': {exc}"}
        except Exception as exc:
            return {"error": f"MCP invocation error: {exc}"}

    async def stop_server(self, session_id: str, server_name: str) -> None:
        """Terminate a single MCP server subprocess."""
        key = (session_id, server_name)
        proc = self._processes.pop(key, None)
        if proc is None:
            return
        await self._terminate(proc)

    async def stop_all(self, session_id: str) -> None:
        """Terminate all MCP server subprocesses for a session."""
        keys_to_remove = [k for k in self._processes if k[0] == session_id]
        for key in keys_to_remove:
            proc = self._processes.pop(key)
            await self._terminate(proc)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """Gracefully terminate a subprocess."""
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
