"""Unit tests for the orchestrator module.

Mocks DB, provider, sandbox, and verifies:
- Text response emits agent.message and transitions to idle
- Tool use with always_allow executes immediately
- Tool use with always_ask emits requires_action
- Thinking blocks emit agent.thinking
- Denied tools emit error result
- Session in terminated state exits the loop
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.models import (
    Agent,
    BuiltinToolConfig,
    BuiltinToolItemConfig,
    CustomToolConfig,
    ModelConfig,
)
from app.orchestrator import (
    build_context,
    build_tool_definitions,
    dispatch_tool,
    run_session,
)
from app.providers import ContentBlock, ModelResponse
from tests.conftest import setup_mock_provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SESSION_ID = str(uuid.uuid4())
_AGENT_ID = str(uuid.uuid4())
_ENV_ID = str(uuid.uuid4())
_CONTAINER_ID = "container-abc123"


def _make_agent(tools=None) -> Agent:
    return Agent(
        id=_AGENT_ID,
        name="test-agent",
        version=1,
        model=ModelConfig(provider="anthropic", id="claude-sonnet-4-20250514"),
        system="You are helpful.",
        tools=tools or [],
        mcp_servers=[],
        created_at=datetime.now(timezone.utc),
    )


def _make_session_row(status="idle"):
    return {
        "id": uuid.UUID(_SESSION_ID),
        "agent_id": uuid.UUID(_AGENT_ID),
        "agent_version": 1,
        "environment_id": uuid.UUID(_ENV_ID),
        "status": status,
        "container_id": _CONTAINER_ID,
        "title": None,
        "metadata": {},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "archived_at": None,
        "last_event_cursor": None,
        "ttl_seconds": None,
        "stats": {"total_events": 0, "tool_calls": 0, "model_turns": 0},
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _make_agent_row():
    return {
        "id": uuid.UUID(_AGENT_ID),
        "name": "test-agent",
        "version": 1,
        "model": {"provider": "anthropic", "id": "claude-sonnet-4-20250514"},
        "system": "You are helpful.",
        "tools": [],
        "mcp_servers": [],
        "created_at": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------------------
# build_context tests
# ---------------------------------------------------------------------------


class TestBuildContext:
    """Tests for build_context — converting event log to messages."""

    @pytest.mark.asyncio
    async def test_system_message_first(self):
        agent = _make_agent()
        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=[]):
            messages = await build_context(_SESSION_ID, agent)
        assert len(messages) == 1
        assert messages[0] == {"role": "system", "content": "You are helpful."}

    @pytest.mark.asyncio
    async def test_user_message_event(self):
        agent = _make_agent()
        rows = [
            {
                "type": "user.message",
                "payload": {"content": "Hello"},
                "seq": 1,
            }
        ]
        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=rows):
            messages = await build_context(_SESSION_ID, agent)
        assert len(messages) == 2
        assert messages[1] == {"role": "user", "content": "Hello"}

    @pytest.mark.asyncio
    async def test_agent_message_event(self):
        agent = _make_agent()
        rows = [
            {"type": "agent.message", "payload": {"content": "Hi there"}, "seq": 1},
        ]
        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=rows):
            messages = await build_context(_SESSION_ID, agent)
        assert messages[1] == {"role": "assistant", "content": "Hi there"}

    @pytest.mark.asyncio
    async def test_tool_use_and_result_events(self):
        agent = _make_agent()
        rows = [
            {
                "type": "agent.tool_use",
                "payload": {"tool_use_id": "tu1", "name": "bash", "input": {"command": "ls"}},
                "seq": 1,
            },
            {
                "type": "agent.tool_result",
                "payload": {"tool_use_id": "tu1", "result": "file.txt"},
                "seq": 2,
            },
        ]
        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=rows):
            messages = await build_context(_SESSION_ID, agent)
        # system + tool_use + tool_result
        assert len(messages) == 3
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"][0]["type"] == "tool_use"
        assert messages[2]["role"] == "user"
        assert messages[2]["content"][0]["type"] == "tool_result"

    @pytest.mark.asyncio
    async def test_tool_result_error(self):
        agent = _make_agent()
        rows = [
            {
                "type": "agent.tool_result",
                "payload": {"tool_use_id": "tu1", "error": "denied"},
                "seq": 1,
            },
        ]
        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=rows):
            messages = await build_context(_SESSION_ID, agent)
        assert messages[1]["content"][0]["is_error"] is True

    @pytest.mark.asyncio
    async def test_empty_system_prompt(self):
        agent = _make_agent()
        agent.system = ""
        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=[]):
            messages = await build_context(_SESSION_ID, agent)
        assert len(messages) == 0


# ---------------------------------------------------------------------------
# build_tool_definitions tests
# ---------------------------------------------------------------------------


class TestBuildToolDefinitions:
    def test_builtin_tools(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", permission_policy="always_allow"),
                    BuiltinToolItemConfig(name="read", permission_policy="always_ask"),
                ],
            ),
        ])
        defs = build_tool_definitions(agent)
        assert len(defs) == 2
        names = {d["name"] for d in defs}
        assert names == {"bash", "read"}

    def test_disabled_builtin_excluded(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", enabled=False),
                    BuiltinToolItemConfig(name="read", enabled=True),
                ],
            ),
        ])
        defs = build_tool_definitions(agent)
        assert len(defs) == 1
        assert defs[0]["name"] == "read"

    def test_custom_tools(self):
        agent = _make_agent(tools=[
            CustomToolConfig(type="custom", name="deploy", description="Deploy app"),
        ])
        defs = build_tool_definitions(agent)
        assert len(defs) == 1
        assert defs[0]["name"] == "deploy"
        assert defs[0]["description"] == "Deploy app"

    def test_empty_tools(self):
        agent = _make_agent(tools=[])
        defs = build_tool_definitions(agent)
        assert defs == []


# ---------------------------------------------------------------------------
# run_session tests
# ---------------------------------------------------------------------------


def _listen_once():
    """Return an async generator that yields one notification then stops."""
    async def _gen(channel):
        yield "new_events"
    return _gen


def _listen_never():
    """Return an async generator that never yields (for terminated sessions)."""
    async def _gen(channel):
        return
        yield  # make it an async generator
    return _gen


class TestRunSessionTextResponse:
    """Text response → agent.message + transition to idle."""

    @pytest.mark.asyncio
    async def test_text_response_emits_message_and_idles(self):
        agent = _make_agent()
        session_row = _make_session_row(status="idle")

        text_response = ModelResponse(
            content=[ContentBlock(type="text", text="Hello!")],
            stop_reason="end_turn",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=text_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        transitions = []

        async def mock_transition(sid, status):
            transitions.append(status)

        # After first iteration, set session to terminated so loop exits
        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", side_effect=mock_transition),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.build_tool_definitions", return_value=[]),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hi"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        # Verify agent.message was emitted
        event_types = [e[0] for e in appended_events]
        assert "agent.message" in event_types

        # Verify transition to idle
        assert "idle" in transitions

        # Verify session.status_idle was emitted
        assert "session.status_idle" in event_types


class TestRunSessionToolUseAlwaysAllow:
    """Tool use with always_allow → execute immediately."""

    @pytest.mark.asyncio
    async def test_always_allow_executes_tool(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", permission_policy="always_allow"),
                ],
            ),
        ])
        session_row = _make_session_row(status="idle")

        tool_response = ModelResponse(
            content=[
                ContentBlock(
                    type="tool_use",
                    tool_use_id="tu1",
                    tool_name="bash",
                    tool_input={"command": "ls"},
                ),
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=tool_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        mock_dispatch = AsyncMock(return_value=('{"stdout": "file.txt", "stderr": "", "exit_code": 0}', "agent.tool_result"))

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", new_callable=AsyncMock),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.dispatch_tool", mock_dispatch),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Run ls"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        # Verify agent.tool_use was emitted
        event_types = [e[0] for e in appended_events]
        assert "agent.tool_use" in event_types

        # Verify agent.tool_result was emitted (tool was executed)
        assert "agent.tool_result" in event_types

        # Verify dispatch_tool was called
        mock_dispatch.assert_called_once()


class TestRunSessionToolUseAlwaysAsk:
    """Tool use with always_ask → emit requires_action."""

    @pytest.mark.asyncio
    async def test_always_ask_emits_requires_action(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", permission_policy="always_ask"),
                ],
            ),
        ])
        session_row = _make_session_row(status="idle")

        tool_response = ModelResponse(
            content=[
                ContentBlock(
                    type="tool_use",
                    tool_use_id="tu1",
                    tool_name="bash",
                    tool_input={"command": "rm -rf /"},
                ),
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=tool_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        # Simulate user approving the tool call
        mock_wait = AsyncMock(return_value={"result": "approve", "tool_use_id": "tu1"})
        mock_dispatch = AsyncMock(return_value=('{"stdout": "done", "stderr": "", "exit_code": 0}', "agent.tool_result"))

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", new_callable=AsyncMock),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.wait_for_confirmation", mock_wait),
            patch("app.orchestrator.dispatch_tool", mock_dispatch),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Do something"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        event_types = [e[0] for e in appended_events]
        assert "agent.tool_use" in event_types
        assert "session.requires_action" in event_types
        # Tool was approved, so result should be emitted
        assert "agent.tool_result" in event_types



class TestRunSessionToolUseDenied:
    """Tool use with denied permission → emit error result."""

    @pytest.mark.asyncio
    async def test_denied_tool_emits_error(self):
        # Agent has no tools configured → everything is denied
        agent = _make_agent(tools=[])
        session_row = _make_session_row(status="idle")

        tool_response = ModelResponse(
            content=[
                ContentBlock(
                    type="tool_use",
                    tool_use_id="tu1",
                    tool_name="bash",
                    tool_input={"command": "ls"},
                ),
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=tool_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", new_callable=AsyncMock),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.build_tool_definitions", return_value=[]),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Run ls"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        event_types = [e[0] for e in appended_events]
        assert "agent.tool_use" in event_types
        assert "agent.tool_result" in event_types

        # Find the tool_result event and verify it has an error
        tool_results = [e for e in appended_events if e[0] == "agent.tool_result"]
        assert len(tool_results) == 1
        assert "error" in tool_results[0][1]
        assert "not available" in tool_results[0][1]["error"]


class TestRunSessionThinking:
    """Thinking blocks → emit agent.thinking."""

    @pytest.mark.asyncio
    async def test_thinking_emits_event(self):
        agent = _make_agent()
        session_row = _make_session_row(status="idle")

        response = ModelResponse(
            content=[
                ContentBlock(type="thinking", text="Let me think..."),
                ContentBlock(type="text", text="Here's my answer."),
            ],
            stop_reason="end_turn",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", new_callable=AsyncMock),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.build_tool_definitions", return_value=[]),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Think about this"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        event_types = [e[0] for e in appended_events]
        assert "agent.thinking" in event_types
        assert "agent.message" in event_types

        # Verify thinking payload
        thinking_events = [e for e in appended_events if e[0] == "agent.thinking"]
        assert thinking_events[0][1]["text"] == "Let me think..."


class TestRunSessionTerminated:
    """Session in terminated state → exits the loop immediately."""

    @pytest.mark.asyncio
    async def test_terminated_session_exits(self):
        session_row = _make_session_row(status="terminated")

        sandbox = MagicMock()

        with patch("app.orchestrator.load_session", new_callable=AsyncMock, return_value=session_row):
            # Should return without error
            await run_session(_SESSION_ID, sandbox)


class TestRunSessionFailed:
    """Session in failed state → exits the loop immediately."""

    @pytest.mark.asyncio
    async def test_failed_session_exits(self):
        session_row = _make_session_row(status="failed")

        sandbox = MagicMock()

        with patch("app.orchestrator.load_session", new_callable=AsyncMock, return_value=session_row):
            await run_session(_SESSION_ID, sandbox)


class TestRunSessionToolConfirmationDenied:
    """Tool use with always_ask + user denies → emit error result."""

    @pytest.mark.asyncio
    async def test_denied_confirmation_emits_error(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", permission_policy="always_ask"),
                ],
            ),
        ])
        session_row = _make_session_row(status="idle")

        tool_response = ModelResponse(
            content=[
                ContentBlock(
                    type="tool_use",
                    tool_use_id="tu1",
                    tool_name="bash",
                    tool_input={"command": "rm -rf /"},
                ),
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=tool_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        # User denies the tool call
        mock_wait = AsyncMock(return_value={
            "result": "deny",
            "tool_use_id": "tu1",
            "deny_message": "Too dangerous",
        })

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", new_callable=AsyncMock),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.wait_for_confirmation", mock_wait),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Do something dangerous"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        event_types = [e[0] for e in appended_events]
        assert "session.requires_action" in event_types
        assert "agent.tool_result" in event_types

        # Verify the error message from denial
        tool_results = [e for e in appended_events if e[0] == "agent.tool_result"]
        assert "error" in tool_results[0][1]
        assert "Too dangerous" in tool_results[0][1]["error"]


class TestRunSessionProviderError:
    """Provider error → emit session.error, transition to failed."""

    @pytest.mark.asyncio
    async def test_provider_error_fails_session(self):
        from app.providers import ProviderError

        agent = _make_agent()
        session_row = _make_session_row(status="idle")

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, error=ProviderError("API down"))

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        transitions = []

        async def mock_transition(sid, status):
            transitions.append(status)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            return session_row

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", side_effect=mock_transition),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.build_tool_definitions", return_value=[]),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hi"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        event_types = [e[0] for e in appended_events]
        assert "session.error" in event_types
        assert "failed" in transitions


# ---------------------------------------------------------------------------
# Tests for connector tool routing (task 10.4)
# Requirements: 16.2, 16.4, 17.1, 17.3
# ---------------------------------------------------------------------------


class TestClassifyTool:
    """Tests for classify_tool — determining tool type from agent config."""

    def test_builtin_tool(self):
        from app.orchestrator import classify_tool

        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[BuiltinToolItemConfig(name="bash", permission_policy="always_allow")],
            ),
        ])
        assert classify_tool("bash", agent) == "builtin"

    def test_mcp_tool(self):
        from app.models import MCPServerConfig
        from app.orchestrator import classify_tool

        agent = _make_agent()
        agent.mcp_servers = [
            MCPServerConfig(name="github", command="npx", args=["@github/mcp"]),
        ]
        assert classify_tool("github__list_repos", agent) == "mcp"

    def test_custom_http_tool(self):
        from app.orchestrator import classify_tool

        agent = _make_agent(tools=[
            CustomToolConfig(type="custom", name="deploy", description="Deploy app"),
        ])
        assert classify_tool("deploy", agent) == "custom_http"

    def test_unknown_tool_defaults_to_builtin(self):
        from app.orchestrator import classify_tool

        agent = _make_agent(tools=[])
        assert classify_tool("unknown_tool", agent) == "builtin"


class TestGetConnectorUrl:
    """Tests for get_connector_url — reading from env var."""

    def test_default_url(self, monkeypatch):
        from app.orchestrator import get_connector_url

        monkeypatch.delenv("CONNECTOR_URL", raising=False)
        assert get_connector_url() == "http://linchpin-connector:8001"

    def test_custom_url(self, monkeypatch):
        from app.orchestrator import get_connector_url

        monkeypatch.setenv("CONNECTOR_URL", "http://localhost:9999")
        assert get_connector_url() == "http://localhost:9999"


class TestDispatchToolMCP:
    """Tests for dispatch_tool routing MCP tools to the connector."""

    @pytest.mark.asyncio
    async def test_mcp_tool_forwards_to_connector(self):
        from app.models import MCPServerConfig
        from app.orchestrator import dispatch_tool

        agent = _make_agent()
        agent.mcp_servers = [
            MCPServerConfig(name="github", command="npx", args=["@github/mcp"], env={"GITHUB_TOKEN": "tok"}),
        ]

        block = ContentBlock(
            type="tool_use",
            tool_use_id="tu_mcp",
            tool_name="github__list_repos",
            tool_input={"org": "acme"},
        )

        connector_response = {"result": {"repos": ["repo1"]}, "status": "ok"}

        sandbox = MagicMock()

        with patch("app.orchestrator.invoke_connector", new_callable=AsyncMock, return_value=connector_response):
            result, event_type = await dispatch_tool(_SESSION_ID, block, sandbox, agent)

        assert event_type == "agent.mcp_tool_result"
        parsed = json.loads(result)
        assert parsed == {"repos": ["repo1"]}

    @pytest.mark.asyncio
    async def test_mcp_tool_error_from_connector(self):
        from app.models import MCPServerConfig
        from app.orchestrator import dispatch_tool

        agent = _make_agent()
        agent.mcp_servers = [
            MCPServerConfig(name="github", command="npx", args=["@github/mcp"]),
        ]

        block = ContentBlock(
            type="tool_use",
            tool_use_id="tu_mcp_err",
            tool_name="github__list_repos",
            tool_input={},
        )

        connector_response = {"error": "MCP server crashed", "status": "error"}

        sandbox = MagicMock()

        with patch("app.orchestrator.invoke_connector", new_callable=AsyncMock, return_value=connector_response):
            result, event_type = await dispatch_tool(_SESSION_ID, block, sandbox, agent)

        assert event_type == "agent.mcp_tool_result"
        parsed = json.loads(result)
        assert "error" in parsed
        assert "MCP server crashed" in parsed["error"]


class TestDispatchToolCustomHTTP:
    """Tests for dispatch_tool routing custom HTTP tools to the connector."""

    @pytest.mark.asyncio
    async def test_custom_tool_with_endpoint_forwards_to_connector(self):
        from app.orchestrator import dispatch_tool

        agent = _make_agent(tools=[
            CustomToolConfig(
                type="custom",
                name="deploy",
                description="Deploy app",
                input_schema={"type": "object"},
                permission_policy="always_allow",
            ),
        ])
        # Add endpoint attribute
        agent.tools[0].endpoint = "https://deploy.example.com/invoke"

        block = ContentBlock(
            type="tool_use",
            tool_use_id="tu_custom",
            tool_name="deploy",
            tool_input={"env": "production"},
        )

        connector_response = {"result": {"status": "deployed"}, "status": "ok"}

        sandbox = MagicMock()

        with patch("app.orchestrator.invoke_connector", new_callable=AsyncMock, return_value=connector_response):
            result, event_type = await dispatch_tool(_SESSION_ID, block, sandbox, agent)

        assert event_type == "agent.tool_result"
        parsed = json.loads(result)
        assert parsed == {"status": "deployed"}

    @pytest.mark.asyncio
    async def test_custom_tool_without_endpoint_returns_sentinel(self):
        from app.orchestrator import dispatch_tool

        agent = _make_agent(tools=[
            CustomToolConfig(
                type="custom",
                name="human_review",
                description="Ask human for review",
            ),
        ])

        block = ContentBlock(
            type="tool_use",
            tool_use_id="tu_custom_no_ep",
            tool_name="human_review",
            tool_input={"question": "Is this ok?"},
        )

        sandbox = MagicMock()

        result, event_type = await dispatch_tool(_SESSION_ID, block, sandbox, agent)

        assert result == "__custom_tool_needs_client__"
        assert event_type == "agent.custom_tool_use"


class TestDispatchToolBuiltin:
    """Tests for dispatch_tool still routing built-in tools correctly."""

    @pytest.mark.asyncio
    async def test_builtin_tool_executes_via_sandbox(self):
        from app.orchestrator import dispatch_tool

        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[BuiltinToolItemConfig(name="bash", permission_policy="always_allow")],
            ),
        ])

        block = ContentBlock(
            type="tool_use",
            tool_use_id="tu_builtin",
            tool_name="bash",
            tool_input={"command": "ls"},
        )

        session_row = _make_session_row()
        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", new_callable=AsyncMock, return_value=session_row),
            patch("app.orchestrator.execute_builtin_tool", new_callable=AsyncMock, return_value={"stdout": "file.txt", "exit_code": 0}),
        ):
            result, event_type = await dispatch_tool(_SESSION_ID, block, sandbox, agent)

        assert event_type == "agent.tool_result"
        parsed = json.loads(result)
        assert parsed["stdout"] == "file.txt"


class TestInvokeConnector:
    """Tests for invoke_connector — HTTP call to the connector service."""

    @pytest.mark.asyncio
    async def test_successful_mcp_invocation(self):
        from app.orchestrator import invoke_connector

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": {"data": "ok"}, "status": "ok"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.orchestrator.httpx.AsyncClient", return_value=mock_client):
            result = await invoke_connector(
                session_id=_SESSION_ID,
                tool_type="mcp",
                tool_name="list_repos",
                arguments={"org": "acme"},
                server_name="github",
                credentials={"GITHUB_TOKEN": "tok"},
            )

        assert result == {"result": {"data": "ok"}, "status": "ok"}
        # Verify the POST was called with correct payload
        call_args = mock_client.post.call_args
        posted_json = call_args.kwargs.get("json") or call_args[1].get("json")
        assert posted_json["tool_type"] == "mcp"
        assert posted_json["server_name"] == "github"
        assert posted_json["tool_name"] == "list_repos"

    @pytest.mark.asyncio
    async def test_connector_http_error(self):
        from app.orchestrator import invoke_connector

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.orchestrator.httpx.AsyncClient", return_value=mock_client):
            result = await invoke_connector(
                session_id=_SESSION_ID,
                tool_type="mcp",
                tool_name="list_repos",
                arguments={},
                server_name="github",
            )

        assert result["status"] == "error"
        assert "error" in result


class TestRunSessionMCPToolUse:
    """MCP tool use → emit agent.mcp_tool_use + agent.mcp_tool_result."""

    @pytest.mark.asyncio
    async def test_mcp_tool_emits_correct_events(self):
        from app.models import MCPServerConfig

        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="github__list_repos", permission_policy="always_allow"),
                ],
            ),
        ])
        agent.mcp_servers = [
            MCPServerConfig(name="github", command="npx", args=["@github/mcp"]),
        ]
        session_row = _make_session_row(status="idle")

        tool_response = ModelResponse(
            content=[
                ContentBlock(
                    type="tool_use",
                    tool_use_id="tu_mcp1",
                    tool_name="github__list_repos",
                    tool_input={"org": "acme"},
                ),
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=tool_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        mock_dispatch = AsyncMock(return_value=('{"repos": ["repo1"]}', "agent.mcp_tool_result"))

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", new_callable=AsyncMock),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.dispatch_tool", mock_dispatch),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "List repos"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        event_types = [e[0] for e in appended_events]
        # Should emit agent.mcp_tool_use (not agent.tool_use)
        assert "agent.mcp_tool_use" in event_types
        # Should emit agent.mcp_tool_result
        assert "agent.mcp_tool_result" in event_types


class TestRunSessionCustomToolUse:
    """Custom tool use → emit agent.custom_tool_use."""

    @pytest.mark.asyncio
    async def test_custom_tool_with_endpoint_emits_correct_events(self):
        agent = _make_agent(tools=[
            CustomToolConfig(
                type="custom",
                name="deploy",
                description="Deploy app",
                permission_policy="always_allow",
            ),
        ])
        session_row = _make_session_row(status="idle")

        tool_response = ModelResponse(
            content=[
                ContentBlock(
                    type="tool_use",
                    tool_use_id="tu_custom1",
                    tool_name="deploy",
                    tool_input={"env": "prod"},
                ),
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=tool_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        # dispatch_tool returns a result for custom tool with endpoint
        mock_dispatch = AsyncMock(return_value=('{"status": "deployed"}', "agent.tool_result"))

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", new_callable=AsyncMock),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.dispatch_tool", mock_dispatch),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Deploy to prod"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        event_types = [e[0] for e in appended_events]
        # Should emit agent.custom_tool_use (not agent.tool_use)
        assert "agent.custom_tool_use" in event_types
        # Should emit agent.tool_result with the result
        assert "agent.tool_result" in event_types


class TestBuildContextMCPEvents:
    """Tests for build_context handling MCP and custom tool events."""

    @pytest.mark.asyncio
    async def test_mcp_tool_use_in_context(self):
        agent = _make_agent()
        rows = [
            {
                "type": "agent.mcp_tool_use",
                "payload": {"tool_use_id": "tu_mcp", "name": "github__list_repos", "input": {"org": "acme"}},
                "seq": 1,
            },
        ]
        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=rows):
            messages = await build_context(_SESSION_ID, agent)
        # system + mcp_tool_use
        assert len(messages) == 2
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"][0]["type"] == "tool_use"
        assert messages[1]["content"][0]["name"] == "github__list_repos"

    @pytest.mark.asyncio
    async def test_mcp_tool_result_in_context(self):
        agent = _make_agent()
        rows = [
            {
                "type": "agent.mcp_tool_result",
                "payload": {"tool_use_id": "tu_mcp", "result": {"repos": ["r1"]}},
                "seq": 1,
            },
        ]
        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=rows):
            messages = await build_context(_SESSION_ID, agent)
        assert len(messages) == 2
        assert messages[1]["role"] == "user"
        assert messages[1]["content"][0]["type"] == "tool_result"

    @pytest.mark.asyncio
    async def test_mcp_tool_result_error_in_context(self):
        agent = _make_agent()
        rows = [
            {
                "type": "agent.mcp_tool_result",
                "payload": {"tool_use_id": "tu_mcp", "error": "server crashed"},
                "seq": 1,
            },
        ]
        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=rows):
            messages = await build_context(_SESSION_ID, agent)
        assert messages[1]["content"][0]["is_error"] is True

    @pytest.mark.asyncio
    async def test_custom_tool_use_in_context(self):
        agent = _make_agent()
        rows = [
            {
                "type": "agent.custom_tool_use",
                "payload": {"tool_use_id": "tu_custom", "name": "deploy", "input": {}},
                "seq": 1,
            },
        ]
        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=rows):
            messages = await build_context(_SESSION_ID, agent)
        assert len(messages) == 2
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"][0]["type"] == "tool_use"



# ---------------------------------------------------------------------------
# Tests for task 11.1: User interrupt handling
# Requirements: 9.3, 6.5
# ---------------------------------------------------------------------------


class TestCheckForInterrupt:
    """Tests for check_for_interrupt — detecting user.interrupt events."""

    @pytest.mark.asyncio
    async def test_interrupt_detected(self):
        from app.orchestrator import check_for_interrupt

        row = {"type": "user.interrupt"}
        with patch("app.orchestrator.fetch_one", new_callable=AsyncMock, return_value=row):
            assert await check_for_interrupt(_SESSION_ID) is True

    @pytest.mark.asyncio
    async def test_no_interrupt_when_other_event(self):
        from app.orchestrator import check_for_interrupt

        row = {"type": "user.message"}
        with patch("app.orchestrator.fetch_one", new_callable=AsyncMock, return_value=row):
            assert await check_for_interrupt(_SESSION_ID) is False

    @pytest.mark.asyncio
    async def test_no_interrupt_when_no_events(self):
        from app.orchestrator import check_for_interrupt

        with patch("app.orchestrator.fetch_one", new_callable=AsyncMock, return_value=None):
            assert await check_for_interrupt(_SESSION_ID) is False


class TestHandleInterrupt:
    """Tests for handle_interrupt — transitioning to idle on interrupt."""

    @pytest.mark.asyncio
    async def test_transitions_to_idle_with_interrupt_reason(self):
        from app.orchestrator import handle_interrupt

        transitions = []
        appended = []

        async def mock_transition(sid, status):
            transitions.append(status)

        async def mock_append(sid, etype, payload):
            appended.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        with (
            patch("app.orchestrator.transition", side_effect=mock_transition),
            patch("app.orchestrator.append_event", side_effect=mock_append),
        ):
            await handle_interrupt(_SESSION_ID)

        assert transitions == ["idle"]
        assert len(appended) == 1
        assert appended[0][0] == "session.status_idle"
        assert appended[0][1]["stop_reason"] == "interrupt"


class TestRunSessionInterrupt:
    """User interrupt → cancel processing, transition to idle."""

    @pytest.mark.asyncio
    async def test_interrupt_before_model_call(self):
        """When user.interrupt is the latest event after waking, skip model call."""
        session_row = _make_session_row(status="idle")

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        transitions = []

        async def mock_transition(sid, status):
            transitions.append(status)

        # check_for_interrupt returns True on first call, then session terminates
        interrupt_calls = 0
        async def mock_check_interrupt(sid):
            nonlocal interrupt_calls
            interrupt_calls += 1
            return interrupt_calls == 1

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", side_effect=mock_transition),
            patch("app.orchestrator.check_for_interrupt", side_effect=mock_check_interrupt),
        ):
            await run_session(_SESSION_ID, sandbox)

        # Should have transitioned to idle (from interrupt)
        assert "idle" in transitions
        event_types = [e[0] for e in appended_events]
        assert "session.status_idle" in event_types
        # Verify stop_reason is "interrupt"
        idle_events = [e for e in appended_events if e[0] == "session.status_idle"]
        assert idle_events[0][1]["stop_reason"] == "interrupt"

    @pytest.mark.asyncio
    async def test_interrupt_during_tool_execution(self):
        """When user.interrupt arrives between tool calls, stop processing."""
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", permission_policy="always_allow"),
                ],
            ),
        ])
        session_row = _make_session_row(status="idle")

        # Model returns two tool calls
        tool_response = ModelResponse(
            content=[
                ContentBlock(type="tool_use", tool_use_id="tu1", tool_name="bash", tool_input={"command": "ls"}),
                ContentBlock(type="tool_use", tool_use_id="tu2", tool_name="bash", tool_input={"command": "pwd"}),
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=tool_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        # First check_for_interrupt (before model call) returns False
        # Second check_for_interrupt (before first tool block) returns False
        # Third check_for_interrupt (before second tool block) returns True
        interrupt_check_count = 0
        async def mock_check_interrupt(sid):
            nonlocal interrupt_check_count
            interrupt_check_count += 1
            return interrupt_check_count == 3

        mock_dispatch = AsyncMock(return_value=('{"stdout": "file.txt"}', "agent.tool_result"))

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", new_callable=AsyncMock),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.dispatch_tool", mock_dispatch),
            patch("app.orchestrator.check_for_interrupt", side_effect=mock_check_interrupt),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Run commands"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        event_types = [e[0] for e in appended_events]
        # First tool should have been processed
        assert "agent.tool_use" in event_types
        assert "agent.tool_result" in event_types
        # Interrupt should have been handled
        assert "session.status_idle" in event_types


# ---------------------------------------------------------------------------
# Tests for task 11.2: Tool confirmation flow
# Requirements: 9.2, 11.5, 6.7
# ---------------------------------------------------------------------------


class TestToolConfirmationFlowAllow:
    """Verify always_ask → requires_action → allow → tool executes."""

    @pytest.mark.asyncio
    async def test_confirmation_allow_proceeds(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", permission_policy="always_ask"),
                ],
            ),
        ])
        session_row = _make_session_row(status="idle")

        tool_response = ModelResponse(
            content=[
                ContentBlock(type="tool_use", tool_use_id="tu1", tool_name="bash", tool_input={"command": "ls"}),
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=tool_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        # User allows the tool call
        mock_wait = AsyncMock(return_value={"result": "allow", "tool_use_id": "tu1"})
        mock_dispatch = AsyncMock(return_value=('{"stdout": "file.txt"}', "agent.tool_result"))

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", new_callable=AsyncMock),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.wait_for_confirmation", mock_wait),
            patch("app.orchestrator.dispatch_tool", mock_dispatch),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Run ls"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        event_types = [e[0] for e in appended_events]
        # Verify the flow: tool_use → requires_action → tool_result
        assert "agent.tool_use" in event_types
        assert "session.requires_action" in event_types
        assert "agent.tool_result" in event_types
        # Tool was actually executed
        mock_dispatch.assert_called_once()


class TestToolConfirmationFlowDeny:
    """Verify always_ask → requires_action → deny → error result."""

    @pytest.mark.asyncio
    async def test_confirmation_deny_emits_error(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", permission_policy="always_ask"),
                ],
            ),
        ])
        session_row = _make_session_row(status="idle")

        tool_response = ModelResponse(
            content=[
                ContentBlock(type="tool_use", tool_use_id="tu1", tool_name="bash", tool_input={"command": "rm -rf /"}),
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=tool_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            return {**session_row, "status": "terminated"}

        # User denies the tool call
        mock_wait = AsyncMock(return_value={
            "result": "deny",
            "tool_use_id": "tu1",
            "deny_message": "Not safe",
        })

        sandbox = MagicMock()

        with (
            patch("app.orchestrator.load_session", side_effect=mock_load_session),
            patch("app.orchestrator.load_agent", new_callable=AsyncMock, return_value=agent),
            patch("app.orchestrator.listen", side_effect=_listen_once()),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.transition", new_callable=AsyncMock),
            patch("app.orchestrator.update_usage", new_callable=AsyncMock),
            patch("app.orchestrator.get_provider", return_value=mock_provider),
            patch("app.orchestrator.wait_for_confirmation", mock_wait),
            patch("app.orchestrator.check_for_interrupt", new_callable=AsyncMock, return_value=False),
            patch("app.orchestrator.build_context", new_callable=AsyncMock, return_value=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Do something dangerous"},
            ]),
        ):
            await run_session(_SESSION_ID, sandbox)

        event_types = [e[0] for e in appended_events]
        assert "session.requires_action" in event_types
        assert "agent.tool_result" in event_types

        # Verify the error message
        tool_results = [e for e in appended_events if e[0] == "agent.tool_result"]
        assert "error" in tool_results[0][1]
        assert "Not safe" in tool_results[0][1]["error"]


# ---------------------------------------------------------------------------
# Tests for task 11.3: Session TTL and cleanup
# Requirements: 19.1, 19.2
# ---------------------------------------------------------------------------


class TestCleanupExpiredSessions:
    """Tests for cleanup_expired_sessions background task."""

    @pytest.mark.asyncio
    async def test_expired_session_is_terminated(self):
        import asyncio
        from app.orchestrator import cleanup_expired_sessions

        session_id = str(uuid.uuid4())
        container_id = "container-expired"

        expired_rows = [
            {"id": uuid.UUID(session_id), "container_id": container_id},
        ]

        transitions = []
        appended = []

        async def mock_transition(sid, status):
            transitions.append((sid, status))

        async def mock_append(sid, etype, payload):
            appended.append((sid, etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        sandbox = MagicMock()
        sandbox.destroy = AsyncMock()

        orchestrator_tasks = {}
        mock_task = MagicMock()
        mock_task.done.return_value = False
        mock_task.cancel = MagicMock()
        orchestrator_tasks[session_id] = mock_task

        call_count = 0
        async def mock_fetch_all(query, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return expired_rows
            return []

        # Run cleanup once then cancel
        async def run_once():
            task = asyncio.create_task(
                cleanup_expired_sessions(sandbox, orchestrator_tasks, interval_seconds=0)
            )
            # Give it time to run one iteration
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with (
            patch("app.orchestrator.fetch_all", side_effect=mock_fetch_all),
            patch("app.orchestrator.transition", side_effect=mock_transition),
            patch("app.orchestrator.append_event", side_effect=mock_append),
        ):
            await run_once()

        # Verify session was terminated
        assert (session_id, "terminated") in transitions

        # Verify session.status_terminated event was emitted
        terminated_events = [e for e in appended if e[1] == "session.status_terminated"]
        assert len(terminated_events) == 1
        assert terminated_events[0][2]["reason"] == "ttl_expired"

        # Verify container was destroyed
        sandbox.destroy.assert_called_once_with(container_id)

        # Verify orchestrator task was cancelled
        mock_task.cancel.assert_called_once()

        # Verify task was removed from dict
        assert session_id not in orchestrator_tasks

    @pytest.mark.asyncio
    async def test_no_expired_sessions(self):
        import asyncio
        from app.orchestrator import cleanup_expired_sessions

        sandbox = MagicMock()
        sandbox.destroy = AsyncMock()
        orchestrator_tasks = {}

        async def mock_fetch_all(query, *args):
            return []

        async def run_once():
            task = asyncio.create_task(
                cleanup_expired_sessions(sandbox, orchestrator_tasks, interval_seconds=0)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with patch("app.orchestrator.fetch_all", side_effect=mock_fetch_all):
            await run_once()

        # No containers should be destroyed
        sandbox.destroy.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_session_without_container(self):
        import asyncio
        from app.orchestrator import cleanup_expired_sessions

        session_id = str(uuid.uuid4())

        expired_rows = [
            {"id": uuid.UUID(session_id), "container_id": None},
        ]

        async def mock_transition(sid, status):
            pass

        async def mock_append(sid, etype, payload):
            return MagicMock(session_id=sid, cursor="c1", seq=1, type=etype, payload=payload, processed_at=None)

        sandbox = MagicMock()
        sandbox.destroy = AsyncMock()
        orchestrator_tasks = {}

        call_count = 0
        async def mock_fetch_all(query, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return expired_rows
            return []

        async def run_once():
            task = asyncio.create_task(
                cleanup_expired_sessions(sandbox, orchestrator_tasks, interval_seconds=0)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with (
            patch("app.orchestrator.fetch_all", side_effect=mock_fetch_all),
            patch("app.orchestrator.transition", side_effect=mock_transition),
            patch("app.orchestrator.append_event", side_effect=mock_append),
        ):
            await run_once()

        # Container destroy should NOT be called when container_id is None
        sandbox.destroy.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for task 12.1: Session recovery on startup
# Requirements: 23.1, 23.2, 23.3, 23.4, 6.8
# ---------------------------------------------------------------------------


class TestRecoverSessions:
    """Tests for recover_sessions — recovering non-terminal sessions on startup."""

    @pytest.mark.asyncio
    async def test_recovers_session_with_live_container(self):
        """Session with a live container → rescheduling → idle, orchestrator started."""
        from app.orchestrator import recover_sessions
        from app.sandbox import ExecResult

        session_row = _make_session_row(status="running")
        sandbox = MagicMock()
        sandbox.exec = AsyncMock(return_value=ExecResult(stdout="", stderr="", exit_code=0))

        orchestrator_tasks: dict = {}
        appended_events: list = []
        transitions: list = []

        async def mock_append(sid, etype, payload):
            appended_events.append((sid, etype, payload))
            return MagicMock()

        async def mock_transition(sid, status):
            transitions.append((sid, status))

        with (
            patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=[session_row]),
            patch("app.orchestrator.transition", side_effect=mock_transition),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.run_session", new_callable=AsyncMock) as mock_run,
        ):
            await recover_sessions(sandbox, orchestrator_tasks)

        sid = str(session_row["id"])

        # Should transition to rescheduling first, then idle
        assert (sid, "rescheduling") in transitions
        assert (sid, "idle") in transitions

        # Should emit session.status_rescheduled
        rescheduled = [e for e in appended_events if e[1] == "session.status_rescheduled"]
        assert len(rescheduled) == 1
        assert rescheduled[0][2]["reason"] == "process_restart"

        # Should have started an orchestrator task
        assert sid in orchestrator_tasks

        # Container exec should have been called to verify container is alive
        sandbox.exec.assert_called_once_with(_CONTAINER_ID, "true")

    @pytest.mark.asyncio
    async def test_fails_session_with_missing_container(self):
        """Session whose container is gone → rescheduling → failed."""
        from app.orchestrator import recover_sessions
        from app.sandbox import SandboxError

        session_row = _make_session_row(status="idle")
        sandbox = MagicMock()
        sandbox.exec = AsyncMock(side_effect=SandboxError("Container not found"))

        orchestrator_tasks: dict = {}
        appended_events: list = []
        transitions: list = []

        async def mock_append(sid, etype, payload):
            appended_events.append((sid, etype, payload))
            return MagicMock()

        async def mock_transition(sid, status):
            transitions.append((sid, status))

        with (
            patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=[session_row]),
            patch("app.orchestrator.transition", side_effect=mock_transition),
            patch("app.orchestrator.append_event", side_effect=mock_append),
        ):
            await recover_sessions(sandbox, orchestrator_tasks)

        sid = str(session_row["id"])

        # Should transition to rescheduling, then failed
        assert (sid, "rescheduling") in transitions
        assert (sid, "failed") in transitions

        # Should emit session.status_rescheduled and session.error
        event_types = [e[1] for e in appended_events]
        assert "session.status_rescheduled" in event_types
        assert "session.error" in event_types

        # Error event should mention container not found
        error_events = [e for e in appended_events if e[1] == "session.error"]
        assert "Container not found" in error_events[0][2]["error"]
        assert error_events[0][2]["source"] == "recovery"

        # No orchestrator task should be started
        assert sid not in orchestrator_tasks

    @pytest.mark.asyncio
    async def test_fails_session_with_no_container_id(self):
        """Session with container_id=None → rescheduling → failed."""
        from app.orchestrator import recover_sessions

        session_row = {**_make_session_row(status="running"), "container_id": None}
        sandbox = MagicMock()

        orchestrator_tasks: dict = {}
        appended_events: list = []
        transitions: list = []

        async def mock_append(sid, etype, payload):
            appended_events.append((sid, etype, payload))
            return MagicMock()

        async def mock_transition(sid, status):
            transitions.append((sid, status))

        with (
            patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=[session_row]),
            patch("app.orchestrator.transition", side_effect=mock_transition),
            patch("app.orchestrator.append_event", side_effect=mock_append),
        ):
            await recover_sessions(sandbox, orchestrator_tasks)

        sid = str(session_row["id"])

        # Should transition to rescheduling, then failed
        assert (sid, "rescheduling") in transitions
        assert (sid, "failed") in transitions

        # sandbox.exec should NOT have been called
        sandbox.exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_sessions_to_recover(self):
        """No non-terminal sessions → nothing happens."""
        from app.orchestrator import recover_sessions

        sandbox = MagicMock()
        orchestrator_tasks: dict = {}

        with patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=[]):
            await recover_sessions(sandbox, orchestrator_tasks)

        assert orchestrator_tasks == {}

    @pytest.mark.asyncio
    async def test_recovers_multiple_sessions(self):
        """Multiple sessions recovered — one alive, one dead."""
        from app.orchestrator import recover_sessions
        from app.sandbox import ExecResult, SandboxError

        alive_row = _make_session_row(status="running")
        dead_id = str(uuid.uuid4())
        dead_row = {
            **_make_session_row(status="idle"),
            "id": uuid.UUID(dead_id),
            "container_id": "dead-container",
        }

        call_count = 0

        async def mock_exec(container_id, cmd):
            if container_id == _CONTAINER_ID:
                return ExecResult(stdout="", stderr="", exit_code=0)
            raise SandboxError("Container not found")

        sandbox = MagicMock()
        sandbox.exec = AsyncMock(side_effect=mock_exec)

        orchestrator_tasks: dict = {}
        transitions: list = []

        async def mock_transition(sid, status):
            transitions.append((sid, status))

        async def mock_append(sid, etype, payload):
            return MagicMock()

        with (
            patch("app.orchestrator.fetch_all", new_callable=AsyncMock, return_value=[alive_row, dead_row]),
            patch("app.orchestrator.transition", side_effect=mock_transition),
            patch("app.orchestrator.append_event", side_effect=mock_append),
            patch("app.orchestrator.run_session", new_callable=AsyncMock),
        ):
            await recover_sessions(sandbox, orchestrator_tasks)

        alive_sid = str(alive_row["id"])
        dead_sid = dead_id

        # Alive session should be idle with orchestrator task
        assert (alive_sid, "idle") in transitions
        assert alive_sid in orchestrator_tasks

        # Dead session should be failed without orchestrator task
        assert (dead_sid, "failed") in transitions
        assert dead_sid not in orchestrator_tasks
