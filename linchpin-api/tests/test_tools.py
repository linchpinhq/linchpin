"""Unit tests for the built-in tools module.

Tests each of the 8 tools: bash, read, write, edit, glob, grep, web_fetch,
web_search — plus parameter validation and error handling.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from app.sandbox import ExecResult, SandboxError
from app.tools import execute_builtin_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONTAINER_ID = "container-test-123"


def _make_sandbox(**overrides) -> AsyncMock:
    """Create a mock DockerSandbox with sensible defaults."""
    sandbox = AsyncMock()
    sandbox.exec = AsyncMock(
        return_value=ExecResult(stdout="", stderr="", exit_code=0)
    )
    sandbox.read_file = AsyncMock(return_value="file content")
    sandbox.write_file = AsyncMock()
    for k, v in overrides.items():
        setattr(sandbox, k, v)
    return sandbox


# ---------------------------------------------------------------------------
# bash tool (Req 13.1)
# ---------------------------------------------------------------------------


class TestBashTool:
    @pytest.mark.asyncio
    async def test_executes_command(self):
        sandbox = _make_sandbox()
        sandbox.exec.return_value = ExecResult(stdout="hello\n", stderr="", exit_code=0)

        result = await execute_builtin_tool(
            "bash", {"command": "echo hello"}, sandbox, CONTAINER_ID
        )

        assert result == {"stdout": "hello\n", "stderr": "", "exit_code": 0}
        sandbox.exec.assert_called_once_with(CONTAINER_ID, "echo hello")

    @pytest.mark.asyncio
    async def test_with_timeout(self):
        sandbox = _make_sandbox()
        sandbox.exec.return_value = ExecResult(stdout="", stderr="", exit_code=0)

        result = await execute_builtin_tool(
            "bash", {"command": "sleep 100", "timeout": 5}, sandbox, CONTAINER_ID
        )

        sandbox.exec.assert_called_once_with(CONTAINER_ID, "timeout 5 sleep 100")
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_missing_command(self):
        sandbox = _make_sandbox()
        result = await execute_builtin_tool("bash", {}, sandbox, CONTAINER_ID)
        assert "error" in result
        assert "command" in result["error"]


# ---------------------------------------------------------------------------
# read tool (Req 13.2)
# ---------------------------------------------------------------------------


class TestReadTool:
    @pytest.mark.asyncio
    async def test_reads_file(self):
        sandbox = _make_sandbox()
        sandbox.read_file.return_value = "hello world"

        result = await execute_builtin_tool(
            "read", {"path": "/tmp/test.txt"}, sandbox, CONTAINER_ID
        )

        assert result == {"content": "hello world"}
        sandbox.read_file.assert_called_once_with(CONTAINER_ID, "/tmp/test.txt")

    @pytest.mark.asyncio
    async def test_missing_path(self):
        sandbox = _make_sandbox()
        result = await execute_builtin_tool("read", {}, sandbox, CONTAINER_ID)
        assert "error" in result
        assert "path" in result["error"]


# ---------------------------------------------------------------------------
# write tool (Req 13.3)
# ---------------------------------------------------------------------------


class TestWriteTool:
    @pytest.mark.asyncio
    async def test_writes_file(self):
        sandbox = _make_sandbox()

        result = await execute_builtin_tool(
            "write",
            {"path": "/tmp/out.txt", "content": "data"},
            sandbox,
            CONTAINER_ID,
        )

        assert result == {"status": "ok"}
        sandbox.write_file.assert_called_once_with(CONTAINER_ID, "/tmp/out.txt", "data")

    @pytest.mark.asyncio
    async def test_missing_path(self):
        sandbox = _make_sandbox()
        result = await execute_builtin_tool(
            "write", {"content": "data"}, sandbox, CONTAINER_ID
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_content(self):
        sandbox = _make_sandbox()
        result = await execute_builtin_tool(
            "write", {"path": "/tmp/out.txt"}, sandbox, CONTAINER_ID
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# edit tool (Req 13.4)
# ---------------------------------------------------------------------------


class TestEditTool:
    @pytest.mark.asyncio
    async def test_replaces_text(self):
        sandbox = _make_sandbox()
        sandbox.read_file.return_value = "hello world"

        result = await execute_builtin_tool(
            "edit",
            {"path": "/tmp/f.txt", "old_text": "world", "new_text": "earth"},
            sandbox,
            CONTAINER_ID,
        )

        assert result == {"status": "ok"}
        sandbox.write_file.assert_called_once_with(
            CONTAINER_ID, "/tmp/f.txt", "hello earth"
        )

    @pytest.mark.asyncio
    async def test_old_text_not_found(self):
        sandbox = _make_sandbox()
        sandbox.read_file.return_value = "hello world"

        result = await execute_builtin_tool(
            "edit",
            {"path": "/tmp/f.txt", "old_text": "missing", "new_text": "x"},
            sandbox,
            CONTAINER_ID,
        )

        assert result == {"error": "old_text not found in file"}
        sandbox.write_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_params(self):
        sandbox = _make_sandbox()
        result = await execute_builtin_tool("edit", {}, sandbox, CONTAINER_ID)
        assert "error" in result

        result = await execute_builtin_tool(
            "edit", {"path": "/f.txt"}, sandbox, CONTAINER_ID
        )
        assert "error" in result

        result = await execute_builtin_tool(
            "edit", {"path": "/f.txt", "old_text": "a"}, sandbox, CONTAINER_ID
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# glob tool (Req 13.5)
# ---------------------------------------------------------------------------


class TestGlobTool:
    @pytest.mark.asyncio
    async def test_returns_matches(self):
        sandbox = _make_sandbox()
        sandbox.exec.return_value = ExecResult(
            stdout="./a.py\n./b.py\n", stderr="", exit_code=0
        )

        result = await execute_builtin_tool(
            "glob", {"pattern": "*.py"}, sandbox, CONTAINER_ID
        )

        assert result == {"matches": ["./a.py", "./b.py"]}

    @pytest.mark.asyncio
    async def test_custom_path(self):
        sandbox = _make_sandbox()
        sandbox.exec.return_value = ExecResult(stdout="", stderr="", exit_code=0)

        await execute_builtin_tool(
            "glob", {"pattern": "*.txt", "path": "/src"}, sandbox, CONTAINER_ID
        )

        sandbox.exec.assert_called_once_with(
            CONTAINER_ID, "find /src -name '*.txt'"
        )

    @pytest.mark.asyncio
    async def test_missing_pattern(self):
        sandbox = _make_sandbox()
        result = await execute_builtin_tool("glob", {}, sandbox, CONTAINER_ID)
        assert "error" in result


# ---------------------------------------------------------------------------
# grep tool (Req 13.6)
# ---------------------------------------------------------------------------


class TestGrepTool:
    @pytest.mark.asyncio
    async def test_returns_matches(self):
        sandbox = _make_sandbox()
        sandbox.exec.return_value = ExecResult(
            stdout="main.py:10:def hello():\nmain.py:20:def world():\n",
            stderr="",
            exit_code=0,
        )

        result = await execute_builtin_tool(
            "grep", {"pattern": "def"}, sandbox, CONTAINER_ID
        )

        assert result == {
            "matches": [
                {"file": "main.py", "line": 10, "text": "def hello():"},
                {"file": "main.py", "line": 20, "text": "def world():"},
            ]
        }

    @pytest.mark.asyncio
    async def test_with_include(self):
        sandbox = _make_sandbox()
        sandbox.exec.return_value = ExecResult(stdout="", stderr="", exit_code=0)

        await execute_builtin_tool(
            "grep",
            {"pattern": "TODO", "path": "/src", "include": "*.py"},
            sandbox,
            CONTAINER_ID,
        )

        sandbox.exec.assert_called_once_with(
            CONTAINER_ID,
            "rg --line-number --no-heading 'TODO' /src --glob '*.py'",
        )

    @pytest.mark.asyncio
    async def test_missing_pattern(self):
        sandbox = _make_sandbox()
        result = await execute_builtin_tool("grep", {}, sandbox, CONTAINER_ID)
        assert "error" in result


# ---------------------------------------------------------------------------
# web_fetch tool (Req 13.7)
# ---------------------------------------------------------------------------


class TestWebFetchTool:
    @pytest.mark.asyncio
    async def test_fetches_url(self):
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.text = "<html>ok</html>"
        mock_response.headers = {"content-type": "text/html"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        sandbox = _make_sandbox()

        with patch("app.tools.httpx.AsyncClient", return_value=mock_client):
            result = await execute_builtin_tool(
                "web_fetch",
                {"url": "https://example.com"},
                sandbox,
                CONTAINER_ID,
            )

        assert result["status_code"] == 200
        assert result["body"] == "<html>ok</html>"

    @pytest.mark.asyncio
    async def test_missing_url(self):
        sandbox = _make_sandbox()
        result = await execute_builtin_tool("web_fetch", {}, sandbox, CONTAINER_ID)
        assert "error" in result
        assert "url" in result["error"]


# ---------------------------------------------------------------------------
# web_search tool (Req 13.8)
# ---------------------------------------------------------------------------


class TestWebSearchTool:
    @pytest.mark.asyncio
    async def test_missing_query_returns_error(self):
        sandbox = _make_sandbox()
        result = await execute_builtin_tool(
            "web_search", {}, sandbox, CONTAINER_ID
        )
        assert "error" in result
        assert "query" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_unconfigured_backend_returns_helpful_error(self, monkeypatch):
        # When TAVILY_API_KEY is unset, surface a clear configuration error
        # rather than a 500 from Tavily — the agent (and operator reading
        # logs) needs to know what to do.
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        sandbox = _make_sandbox()
        result = await execute_builtin_tool(
            "web_search", {"query": "test"}, sandbox, CONTAINER_ID
        )
        assert "error" in result
        assert "TAVILY_API_KEY" in result["error"]

    @pytest.mark.asyncio
    async def test_tavily_success_response_normalizes(self, monkeypatch):
        # Real path: TAVILY_API_KEY set, Tavily returns 200 with results.
        # Output is normalized into {query, answer, results[{title,url,snippet,score}]}
        # so the model doesn't have to learn Tavily's specific JSON shape.
        monkeypatch.setenv("TAVILY_API_KEY", "tav-test")

        class _FakeResp:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "answer": "async standup tools are popular among remote teams",
                    "results": [
                        {
                            "title": "Best async standup tools 2026",
                            "url": "https://example.com/async-standup",
                            "content": "Geekbot, Standuply, and several newer LLM-powered alternatives...",
                            "score": 0.92,
                        },
                    ],
                }

        async def fake_post(self, url, **kwargs):
            assert url == "https://api.tavily.com/search"
            assert kwargs["json"]["api_key"] == "tav-test"
            assert kwargs["json"]["query"] == "async standup tools"
            return _FakeResp()

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
        sandbox = _make_sandbox()
        result = await execute_builtin_tool(
            "web_search",
            {"query": "async standup tools"},
            sandbox,
            CONTAINER_ID,
        )
        assert "error" not in result
        assert result["query"] == "async standup tools"
        assert result["answer"].startswith("async standup tools are popular")
        assert len(result["results"]) == 1
        assert result["results"][0]["title"] == "Best async standup tools 2026"
        assert result["results"][0]["url"] == "https://example.com/async-standup"
        assert result["results"][0]["snippet"].startswith("Geekbot")
        assert result["results"][0]["score"] == 0.92

    @pytest.mark.asyncio
    async def test_tavily_non_200_surfaces_status(self, monkeypatch):
        # Backend errors (rate limit, bad query, downtime) become a tool
        # error the agent can react to. The orchestrator's tool-error
        # recovery loop ensures this doesn't stall the session.
        monkeypatch.setenv("TAVILY_API_KEY", "tav-test")

        class _FakeResp:
            status_code = 429
            text = '{"error": "rate limited"}'

            def json(self):
                return {}

        async def fake_post(self, url, **kwargs):
            return _FakeResp()

        monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
        sandbox = _make_sandbox()
        result = await execute_builtin_tool(
            "web_search", {"query": "test"}, sandbox, CONTAINER_ID
        )
        assert "error" in result
        assert "429" in result["error"]


# ---------------------------------------------------------------------------
# Unknown tool / error handling
# ---------------------------------------------------------------------------


class TestUnknownTool:
    @pytest.mark.asyncio
    async def test_unknown_tool(self):
        sandbox = _make_sandbox()
        result = await execute_builtin_tool(
            "nonexistent", {}, sandbox, CONTAINER_ID
        )
        assert "error" in result
        assert "Unknown" in result["error"]


class TestSandboxError:
    @pytest.mark.asyncio
    async def test_sandbox_error_caught(self):
        sandbox = _make_sandbox()
        sandbox.exec.side_effect = SandboxError("Container gone")

        result = await execute_builtin_tool(
            "bash", {"command": "ls"}, sandbox, CONTAINER_ID
        )

        assert "error" in result
        assert "Container gone" in result["error"]
