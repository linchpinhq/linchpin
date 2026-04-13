"""Tests for MCP manager (linchpin-connector/app/mcp.py)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp import MCPManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_process(
    returncode=None,
    stdout_data: bytes = b"",
    pid: int = 1234,
):
    """Create a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = pid

    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    proc.stdin = stdin

    stdout = MagicMock()
    stdout.readline = AsyncMock(return_value=stdout_data)
    stdout.read = AsyncMock(return_value=stdout_data)
    proc.stdout = stdout

    stderr = MagicMock()
    stderr.read = AsyncMock(return_value=b"")
    proc.stderr = stderr

    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    return proc


# ---------------------------------------------------------------------------
# start_server
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_server_spawns_subprocess():
    mgr = MCPManager()
    mock_proc = _make_mock_process()

    with patch("app.mcp.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        await mgr.start_server("sess-1", "my-server", "node", ["server.js"], {"API_KEY": "secret"})

    assert ("sess-1", "my-server") in mgr._processes
    assert mgr._processes[("sess-1", "my-server")] is mock_proc


@pytest.mark.asyncio
async def test_start_server_skips_if_already_running():
    mgr = MCPManager()
    existing = _make_mock_process()  # returncode=None means running
    mgr._processes[("sess-1", "srv")] = existing

    with patch("app.mcp.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        await mgr.start_server("sess-1", "srv", "node", ["s.js"])
        mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_start_server_replaces_crashed_process():
    mgr = MCPManager()
    crashed = _make_mock_process(returncode=1)
    mgr._processes[("sess-1", "srv")] = crashed

    new_proc = _make_mock_process()
    with patch("app.mcp.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=new_proc):
        await mgr.start_server("sess-1", "srv", "node", ["s.js"])

    assert mgr._processes[("sess-1", "srv")] is new_proc


# ---------------------------------------------------------------------------
# invoke
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoke_returns_result():
    mgr = MCPManager()
    response_data = {"result": {"content": "hello"}}
    proc = _make_mock_process(stdout_data=json.dumps(response_data).encode() + b"\n")
    mgr._processes[("sess-1", "srv")] = proc

    result = await mgr.invoke("sess-1", "srv", "greet", {"name": "world"})

    assert result == {"result": response_data}
    proc.stdin.write.assert_called_once()
    written = proc.stdin.write.call_args[0][0]
    req = json.loads(written.decode())
    assert req["method"] == "tools/call"
    assert req["params"]["name"] == "greet"
    assert req["params"]["arguments"] == {"name": "world"}


@pytest.mark.asyncio
async def test_invoke_server_not_found():
    mgr = MCPManager()
    result = await mgr.invoke("sess-1", "missing", "tool", {})
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_invoke_server_crashed():
    mgr = MCPManager()
    crashed = _make_mock_process(returncode=137)
    mgr._processes[("sess-1", "srv")] = crashed

    result = await mgr.invoke("sess-1", "srv", "tool", {})
    assert "error" in result
    assert "crashed" in result["error"]
    assert "137" in result["error"]
    # Process should be cleaned up
    assert ("sess-1", "srv") not in mgr._processes


@pytest.mark.asyncio
async def test_invoke_timeout():
    mgr = MCPManager(timeout=0.01)
    proc = _make_mock_process()

    # Make readline return a future that never resolves within timeout
    async def _hang():
        await asyncio.sleep(10)
        return b""

    proc.stdout.readline = _hang
    mgr._processes[("sess-1", "srv")] = proc

    result = await mgr.invoke("sess-1", "srv", "slow_tool", {})
    assert "error" in result
    assert "timed out" in result["error"]


@pytest.mark.asyncio
async def test_invoke_invalid_json_response():
    mgr = MCPManager()
    proc = _make_mock_process(stdout_data=b"not json\n")
    mgr._processes[("sess-1", "srv")] = proc

    result = await mgr.invoke("sess-1", "srv", "tool", {})
    assert "error" in result
    assert "Invalid JSON" in result["error"]


@pytest.mark.asyncio
async def test_invoke_empty_response():
    mgr = MCPManager()
    proc = _make_mock_process(stdout_data=b"")
    mgr._processes[("sess-1", "srv")] = proc

    result = await mgr.invoke("sess-1", "srv", "tool", {})
    assert "error" in result
    assert "empty response" in result["error"]


# ---------------------------------------------------------------------------
# stop_server / stop_all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_server():
    mgr = MCPManager()
    proc = _make_mock_process()
    mgr._processes[("sess-1", "srv")] = proc

    await mgr.stop_server("sess-1", "srv")

    assert ("sess-1", "srv") not in mgr._processes
    proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_stop_server_nonexistent_is_noop():
    mgr = MCPManager()
    await mgr.stop_server("sess-1", "nope")  # should not raise


@pytest.mark.asyncio
async def test_stop_all():
    mgr = MCPManager()
    p1 = _make_mock_process()
    p2 = _make_mock_process()
    p3 = _make_mock_process()
    mgr._processes[("sess-1", "a")] = p1
    mgr._processes[("sess-1", "b")] = p2
    mgr._processes[("sess-2", "a")] = p3

    await mgr.stop_all("sess-1")

    assert ("sess-1", "a") not in mgr._processes
    assert ("sess-1", "b") not in mgr._processes
    assert ("sess-2", "a") in mgr._processes  # untouched
    p1.terminate.assert_called_once()
    p2.terminate.assert_called_once()
    p3.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_stop_already_exited_process():
    mgr = MCPManager()
    proc = _make_mock_process(returncode=0)
    mgr._processes[("sess-1", "srv")] = proc

    await mgr.stop_server("sess-1", "srv")
    proc.terminate.assert_not_called()  # already exited, no terminate needed
