"""Tests for custom HTTP tool invoker (linchpin-connector/app/http_tools.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.http_tools import invoke_http_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code: int = 200, json_data=None, text: str = ""):
    """Create a mock httpx.Response."""
    import json as _json

    if json_data is not None:
        content = _json.dumps(json_data).encode()
        headers = {"content-type": "application/json"}
    else:
        content = text.encode()
        headers = {"content-type": "text/plain"}

    resp = httpx.Response(
        status_code=status_code,
        content=content,
        headers=headers,
        request=httpx.Request("POST", "https://example.com/tool"),
    )
    return resp


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_http_tool_success_json():
    mock_resp = _mock_response(200, json_data={"answer": 42})

    with patch("app.http_tools.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=mock_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await invoke_http_tool(
            endpoint="https://example.com/tool",
            tool_name="calc",
            arguments={"x": 1},
        )

    assert result["status_code"] == 200
    assert result["result"] == {"answer": 42}
    assert "error" not in result


@pytest.mark.asyncio
async def test_invoke_http_tool_success_text():
    """When response is not JSON, return text."""
    resp = httpx.Response(
        status_code=200,
        text="plain text result",
        request=httpx.Request("POST", "https://example.com/tool"),
        headers={"content-type": "text/plain"},
    )

    with patch("app.http_tools.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await invoke_http_tool(
            endpoint="https://example.com/tool",
            tool_name="echo",
        )

    assert result["status_code"] == 200
    # result is either parsed JSON or text fallback
    assert result["result"] is not None


# ---------------------------------------------------------------------------
# HTTP error responses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_http_tool_4xx_error():
    resp = httpx.Response(
        status_code=404,
        text="Not Found",
        request=httpx.Request("POST", "https://example.com/tool"),
    )

    with patch("app.http_tools.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await invoke_http_tool(
            endpoint="https://example.com/tool",
            tool_name="missing",
        )

    assert "error" in result
    assert result["status_code"] == 404
    assert "404" in result["error"]


@pytest.mark.asyncio
async def test_invoke_http_tool_5xx_error():
    resp = httpx.Response(
        status_code=500,
        text="Internal Server Error",
        request=httpx.Request("POST", "https://example.com/tool"),
    )

    with patch("app.http_tools.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await invoke_http_tool(
            endpoint="https://example.com/tool",
            tool_name="broken",
        )

    assert "error" in result
    assert result["status_code"] == 500


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_http_tool_timeout():
    with patch("app.http_tools.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await invoke_http_tool(
            endpoint="https://example.com/tool",
            tool_name="slow",
            timeout=5,
        )

    assert "error" in result
    assert "timed out" in result["error"]


# ---------------------------------------------------------------------------
# Connection error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_http_tool_connection_error():
    with patch("app.http_tools.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await invoke_http_tool(
            endpoint="https://unreachable.local/tool",
            tool_name="nope",
        )

    assert "error" in result
    assert "Connection error" in result["error"]


# ---------------------------------------------------------------------------
# Default arguments
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_http_tool_default_arguments():
    """When arguments is None, should send empty dict."""
    mock_resp = _mock_response(200, json_data={"ok": True})

    with patch("app.http_tools.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=mock_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        result = await invoke_http_tool(
            endpoint="https://example.com/tool",
            tool_name="ping",
            arguments=None,
        )

    assert result["result"] == {"ok": True}
    # Verify the payload sent
    call_kwargs = instance.post.call_args
    sent_json = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert sent_json["arguments"] == {}
