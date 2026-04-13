"""Custom HTTP tool invoker for Linchpin Connector.

Makes HTTP requests to user-configured tool endpoints and returns results.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("linchpin-connector.http_tools")

DEFAULT_TIMEOUT = 30  # seconds


async def invoke_http_tool(
    endpoint: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """POST to a custom tool endpoint and return the result.

    Returns a dict with ``result`` and ``status`` keys on success,
    or ``error``, ``status``, and optionally ``status_code`` on failure.
    """
    payload = {"tool_name": tool_name, "arguments": arguments or {}}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint, json=payload)

        if response.status_code >= 400:
            return {
                "error": f"HTTP {response.status_code} from tool endpoint",
                "status_code": response.status_code,
                "body": response.text,
            }

        try:
            result = response.json()
        except Exception:
            result = response.text

        return {"result": result, "status_code": response.status_code}

    except httpx.TimeoutException:
        return {"error": f"Request to '{endpoint}' timed out after {timeout}s"}
    except httpx.ConnectError as exc:
        return {"error": f"Connection error to '{endpoint}': {exc}"}
    except httpx.HTTPError as exc:
        return {"error": f"HTTP error invoking '{endpoint}': {exc}"}
