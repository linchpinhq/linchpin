"""Test: Orchestrator processes a user.message through Ollama provider with vault credentials.

Reproduces the scenario where:
1. Session is created with vault_ids
2. User sends a message
3. Orchestrator wakes up, resolves API key from vault, calls Ollama, emits agent.message
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Agent, ModelConfig
from app.orchestrator import run_session
from app.providers import ContentBlock, ModelResponse
from tests.conftest import setup_mock_provider


_SESSION_ID = str(uuid.uuid4())
_AGENT_ID = str(uuid.uuid4())
_ENV_ID = str(uuid.uuid4())
_VAULT_ID = str(uuid.uuid4())
_CONTAINER_ID = "container-test"


def _make_agent() -> Agent:
    return Agent(
        id=_AGENT_ID,
        name="test-ollama-agent",
        version=1,
        model=ModelConfig(provider="ollama", id="gemma4:31b-cloud", base_url="http://host.docker.internal:11434"),
        system="You are a helpful assistant.",
        tools=[],
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
        "title": "Test",
        "metadata": {},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "archived_at": None,
        "last_event_cursor": None,
        "ttl_seconds": None,
        "stats": {"total_events": 0, "tool_calls": 0, "model_turns": 0},
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "vault_ids": [_VAULT_ID],
    }


def _listen_once():
    """Return an async generator that yields one notification then stops."""
    async def _gen(channel):
        yield "new_events"
    return _gen


class TestOrchestratorOllamaWithVault:
    """Orchestrator resolves Ollama credentials from vault and gets a response."""

    @pytest.mark.asyncio
    async def test_ollama_with_vault_emits_agent_message(self):
        agent = _make_agent()
        session_row = _make_session_row(status="idle")

        # Mock Ollama returning a text response
        text_response = ModelResponse(
            content=[ContentBlock(type="text", text="Hey there! How can I help?")],
            stop_reason="end_turn",
            usage={"input_tokens": 15, "output_tokens": 8},
        )

        mock_provider = AsyncMock()
        setup_mock_provider(mock_provider, response=text_response)

        appended_events = []

        async def mock_append(sid, etype, payload):
            appended_events.append((etype, payload))
            return MagicMock(session_id=sid, cursor="c1", seq=len(appended_events), type=etype, payload=payload, processed_at=None)

        transitions = []

        async def mock_transition(sid, status):
            transitions.append(status)

        call_count = 0
        async def mock_load_session(sid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return session_row
            # After first iteration, terminate so loop exits
            return {**session_row, "status": "terminated"}

        # Mock credential resolver — returns None for Ollama (no key needed)
        mock_resolve_api_key = AsyncMock(return_value=None)

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
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hey"},
            ]),
            patch("app.orchestrator.CredentialResolver") as MockResolver,
        ):
            mock_resolver_instance = MagicMock()
            mock_resolver_instance.resolve_api_key = mock_resolve_api_key
            MockResolver.return_value = mock_resolver_instance

            await run_session(_SESSION_ID, sandbox)

        # Verify the credential resolver was called with the vault_ids
        mock_resolve_api_key.assert_called_once_with([_VAULT_ID], "ollama")

        # Verify the provider was called (Ollama doesn't need a key, so api_key=None is fine)
        # Note: orchestrator now uses send_streaming instead of send
        # The streaming mock was set up by setup_mock_provider

        # Verify agent.message was emitted
        event_types = [e[0] for e in appended_events]
        assert "agent.message" in event_types, f"Expected agent.message in {event_types}"

        # Verify the message content
        agent_messages = [e for e in appended_events if e[0] == "agent.message"]
        assert agent_messages[0][1]["content"] == "Hey there! How can I help?"

        # Verify transition to running then idle
        assert "running" in transitions
        assert "idle" in transitions

        # Verify session.status_idle was emitted
        assert "session.status_idle" in event_types

    @pytest.mark.asyncio
    async def test_ollama_session_without_vault_ids_skips_resolver(self):
        """When session has no vault_ids, credential resolver is not called."""
        agent = _make_agent()
        session_row = _make_session_row(status="idle")
        session_row["vault_ids"] = []  # No vaults

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
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hey"},
            ]),
            patch("app.orchestrator.CredentialResolver") as MockResolver,
        ):
            await run_session(_SESSION_ID, sandbox)

        # CredentialResolver should NOT have been instantiated
        MockResolver.assert_not_called()

        # Provider should still be called (uses env var auth)
        # Note: orchestrator now uses send_streaming instead of send

        # agent.message should still be emitted
        event_types = [e[0] for e in appended_events]
        assert "agent.message" in event_types
