"""Built-in tool implementations for the Linchpin sandbox.

Provides 8 tools: bash, read, write, edit, glob, grep, web_fetch, web_search.
Each tool validates its parameters, executes via the sandbox or httpx, and
returns a result dict.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.sandbox import DockerSandbox, SandboxError

logger = logging.getLogger("linchpin-api.tools")


async def execute_builtin_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    sandbox: DockerSandbox,
    container_id: str,
) -> dict[str, Any]:
    """Execute a built-in tool and return the result dict."""
    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"Unknown built-in tool: {tool_name}"}
    try:
        return await handler(tool_input, sandbox, container_id)
    except SandboxError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.exception("Tool '%s' failed unexpectedly", tool_name)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Individual tool handlers
# ---------------------------------------------------------------------------


async def _bash(
    tool_input: dict[str, Any],
    sandbox: DockerSandbox,
    container_id: str,
) -> dict[str, Any]:
    """Req 13.1 — execute a command inside the container."""
    command = tool_input.get("command")
    if not command:
        return {"error": "Missing required parameter: command"}
    timeout = tool_input.get("timeout")
    if timeout is not None:
        command = f"timeout {int(timeout)} {command}"
    result = await sandbox.exec(container_id, command)
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
    }


async def _read(
    tool_input: dict[str, Any],
    sandbox: DockerSandbox,
    container_id: str,
) -> dict[str, Any]:
    """Req 13.2 — read a file from the container."""
    path = tool_input.get("path")
    if not path:
        return {"error": "Missing required parameter: path"}
    content = await sandbox.read_file(container_id, path)
    return {"content": content}


async def _write(
    tool_input: dict[str, Any],
    sandbox: DockerSandbox,
    container_id: str,
) -> dict[str, Any]:
    """Req 13.3 — write a file inside the container."""
    path = tool_input.get("path")
    content = tool_input.get("content")
    if not path:
        return {"error": "Missing required parameter: path"}
    if content is None:
        return {"error": "Missing required parameter: content"}
    await sandbox.write_file(container_id, path, content)
    return {"status": "ok"}


async def _edit(
    tool_input: dict[str, Any],
    sandbox: DockerSandbox,
    container_id: str,
) -> dict[str, Any]:
    """Req 13.4 — read file, replace old_text with new_text, write back."""
    path = tool_input.get("path")
    old_text = tool_input.get("old_text")
    new_text = tool_input.get("new_text")
    if not path:
        return {"error": "Missing required parameter: path"}
    if old_text is None:
        return {"error": "Missing required parameter: old_text"}
    if new_text is None:
        return {"error": "Missing required parameter: new_text"}

    content = await sandbox.read_file(container_id, path)
    if old_text not in content:
        return {"error": "old_text not found in file"}
    updated = content.replace(old_text, new_text, 1)
    await sandbox.write_file(container_id, path, updated)
    return {"status": "ok"}


async def _glob(
    tool_input: dict[str, Any],
    sandbox: DockerSandbox,
    container_id: str,
) -> dict[str, Any]:
    """Req 13.5 — glob/find pattern match inside the container."""
    pattern = tool_input.get("pattern")
    if not pattern:
        return {"error": "Missing required parameter: pattern"}
    path = tool_input.get("path", ".")
    result = await sandbox.exec(container_id, f"find {path} -name '{pattern}'")
    lines = [l for l in result.stdout.strip().splitlines() if l]
    return {"matches": lines}


async def _grep(
    tool_input: dict[str, Any],
    sandbox: DockerSandbox,
    container_id: str,
) -> dict[str, Any]:
    """Req 13.6 — text search inside the container using rg."""
    pattern = tool_input.get("pattern")
    if not pattern:
        return {"error": "Missing required parameter: pattern"}
    path = tool_input.get("path", ".")
    include = tool_input.get("include")
    cmd = f"rg --line-number --no-heading '{pattern}' {path}"
    if include:
        cmd += f" --glob '{include}'"
    result = await sandbox.exec(container_id, cmd)
    matches: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        # rg output: file:line:text
        parts = line.split(":", 2)
        if len(parts) >= 3:
            matches.append({"file": parts[0], "line": int(parts[1]), "text": parts[2]})
        else:
            matches.append({"file": "", "line": 0, "text": line})
    return {"matches": matches}


async def _web_fetch(
    tool_input: dict[str, Any],
    sandbox: DockerSandbox,
    container_id: str,
) -> dict[str, Any]:
    """Req 13.7 — fetch a URL via httpx (runs in the API process, not sandbox)."""
    url = tool_input.get("url")
    if not url:
        return {"error": "Missing required parameter: url"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
        return {
            "status_code": resp.status_code,
            "body": resp.text,
            "headers": dict(resp.headers),
        }
    except httpx.HTTPError as exc:
        return {"error": f"HTTP request failed: {exc}"}


async def _web_search(
    tool_input: dict[str, Any],
    sandbox: DockerSandbox,
    container_id: str,
) -> dict[str, Any]:
    """Req 13.8 — stub returning not-implemented message."""
    return {
        "error": (
            "web_search is not implemented in MVP. "
            "Configure it as a custom HTTP tool pointing at "
            "Tavily, Brave, SerpAPI, etc."
        )
    }


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

_TOOL_HANDLERS = {
    "bash": _bash,
    "read": _read,
    "write": _write,
    "edit": _edit,
    "glob": _glob,
    "grep": _grep,
    "web_fetch": _web_fetch,
    "web_search": _web_search,
}
