"""Shared test fixtures for linchpin-api tests."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.providers import ContentBlock, ModelResponse, StreamChunk


def model_response_to_stream_chunks(response: ModelResponse):
    """Convert a ModelResponse into a list of StreamChunks for mocking send_streaming.

    This allows existing tests that create ModelResponse objects to also work
    with the streaming orchestrator path.
    """
    chunks: list[StreamChunk] = []
    for block in response.content:
        if block.type == "text":
            chunks.append(StreamChunk(type="text_delta", text=block.text))
        elif block.type == "thinking":
            chunks.append(StreamChunk(type="thinking", text=block.text))
        elif block.type == "tool_use":
            chunks.append(StreamChunk(
                type="tool_use",
                tool_use_id=block.tool_use_id,
                tool_name=block.tool_name,
                tool_input=block.tool_input,
            ))
    chunks.append(StreamChunk(
        type="final",
        stop_reason=response.stop_reason,
        usage=response.usage,
    ))
    return chunks


def make_streaming_mock(response: ModelResponse):
    """Create an async generator function that yields StreamChunks from a ModelResponse."""
    chunks = model_response_to_stream_chunks(response)

    async def _stream(*args, **kwargs):
        for chunk in chunks:
            yield chunk

    return _stream


def setup_mock_provider(mock_provider, response: ModelResponse | None = None, error: Exception | None = None):
    """Configure a mock provider with both send() and send_streaming() methods.

    If error is provided, both methods will raise it.
    If response is provided, send() returns it and send_streaming() yields equivalent chunks.
    """
    if error is not None:
        mock_provider.send = AsyncMock(side_effect=error)

        async def _error_stream(*args, **kwargs):
            raise error
            yield  # make it an async generator  # noqa: E501

        mock_provider.send_streaming = _error_stream
    elif response is not None:
        mock_provider.send = AsyncMock(return_value=response)
        mock_provider.send_streaming = make_streaming_mock(response)


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch):
    """Ensure LINCHPIN_API_KEY is set for every test."""
    monkeypatch.setenv("LINCHPIN_API_KEY", "test-secret-key")
    # v0.2.0 item #14 — skip the webhook delivery worker in tests so it
    # doesn't try to POST anywhere. Individual tests that want to
    # exercise the worker monkeypatch this back on.
    monkeypatch.setenv("LINCHPIN_WEBHOOKS_WORKER", "false")
    # v0.3 PR5 — skip the memory GC cron in tests so the loop doesn't
    # tick during a `with TestClient(app)` lifespan. Tests that
    # exercise GC directly call ``run_memory_gc()``.
    monkeypatch.setenv("LINCHPIN_MEMORY_GC", "false")


@pytest.fixture()
def client():
    """Return a TestClient that skips DB/migration/Docker startup."""
    # Patch out lifespan dependencies so we can test HTTP layer in isolation
    with (
        patch("app.main.check_migrations_current"),
        patch("app.main.create_pool", new_callable=AsyncMock),
        patch("app.main.close_pool", new_callable=AsyncMock),
        patch("app.main.ensure_docker_networks", new_callable=AsyncMock),
        patch("app.main.DockerSandbox") as MockSandbox,
        patch("app.main.cleanup_expired_sessions", new_callable=AsyncMock),
        patch("app.main.recover_sessions", new_callable=AsyncMock),
    ):
        mock_sandbox = MagicMock()
        mock_sandbox.create = AsyncMock(return_value="container-mock")
        mock_sandbox.destroy = AsyncMock()
        # v0.2.0 item #3 — sessions now route image selection through
        # sandbox.ensure_image(). Default behavior in tests is pass-through:
        # whatever base_image the route resolved is what create() sees.
        mock_sandbox.ensure_image = AsyncMock(
            side_effect=lambda *, base_image, packages: base_image
        )
        MockSandbox.return_value = mock_sandbox

        from app.main import app

        with TestClient(app) as c:
            yield c
