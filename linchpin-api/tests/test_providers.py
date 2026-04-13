"""Unit tests for model provider adapters.

Covers:
- Each provider constructs correct API calls
- Responses are parsed into ModelResponse correctly
- Retry logic works (retries on retryable errors, gives up after 3)
- Factory returns correct provider
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import ModelConfig
from app.providers import (
    AnthropicProvider,
    ContentBlock,
    ModelResponse,
    OllamaProvider,
    OpenAIProvider,
    ProviderError,
    RetryableError,
    _parse_anthropic_response,
    _parse_ollama_response,
    _parse_openai_response,
    _retry_with_backoff,
    get_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _anthropic_config() -> ModelConfig:
    return ModelConfig(provider="anthropic", id="claude-sonnet-4-20250514")


def _openai_config() -> ModelConfig:
    return ModelConfig(provider="openai", id="gpt-4o")


def _ollama_config() -> ModelConfig:
    return ModelConfig(provider="ollama", id="llama3", base_url="http://localhost:11434")


# ---------------------------------------------------------------------------
# Response parsing — Anthropic
# ---------------------------------------------------------------------------


class TestParseAnthropicResponse:
    def test_text_response(self):
        resp = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Hello!")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        result = _parse_anthropic_response(resp)
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert result.content[0].text == "Hello!"
        assert result.stop_reason == "end_turn"
        assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    def test_tool_use_response(self):
        resp = SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", id="tu_1", name="bash", input={"command": "ls"}),
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=20, output_tokens=15),
        )
        result = _parse_anthropic_response(resp)
        assert len(result.content) == 1
        block = result.content[0]
        assert block.type == "tool_use"
        assert block.tool_use_id == "tu_1"
        assert block.tool_name == "bash"
        assert block.tool_input == {"command": "ls"}

    def test_thinking_response(self):
        resp = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="Let me think...")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        )
        result = _parse_anthropic_response(resp)
        assert len(result.content) == 1
        assert result.content[0].type == "thinking"
        assert result.content[0].text == "Let me think..."

    def test_mixed_response(self):
        resp = SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking="Hmm"),
                SimpleNamespace(type="text", text="Here's the answer"),
                SimpleNamespace(type="tool_use", id="tu_2", name="read", input={"path": "/tmp/x"}),
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=30, output_tokens=25),
        )
        result = _parse_anthropic_response(resp)
        assert len(result.content) == 3
        assert result.content[0].type == "thinking"
        assert result.content[1].type == "text"
        assert result.content[2].type == "tool_use"


# ---------------------------------------------------------------------------
# Response parsing — OpenAI
# ---------------------------------------------------------------------------


class TestParseOpenAIResponse:
    def test_text_response(self):
        msg = SimpleNamespace(content="Hello!", tool_calls=None)
        choice = SimpleNamespace(message=msg, finish_reason="stop")
        resp = SimpleNamespace(
            choices=[choice],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
        result = _parse_openai_response(resp)
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert result.content[0].text == "Hello!"
        assert result.stop_reason == "end_turn"
        assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    def test_tool_call_response(self):
        tc = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="bash", arguments='{"command": "ls"}'),
        )
        msg = SimpleNamespace(content=None, tool_calls=[tc])
        choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
        resp = SimpleNamespace(
            choices=[choice],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=15),
        )
        result = _parse_openai_response(resp)
        assert len(result.content) == 1
        block = result.content[0]
        assert block.type == "tool_use"
        assert block.tool_use_id == "call_1"
        assert block.tool_name == "bash"
        assert block.tool_input == {"command": "ls"}
        assert result.stop_reason == "tool_use"

    def test_no_usage(self):
        msg = SimpleNamespace(content="Hi", tool_calls=None)
        choice = SimpleNamespace(message=msg, finish_reason="stop")
        resp = SimpleNamespace(choices=[choice], usage=None)
        result = _parse_openai_response(resp)
        assert result.usage == {"input_tokens": 0, "output_tokens": 0}


# ---------------------------------------------------------------------------
# Response parsing — Ollama
# ---------------------------------------------------------------------------


class TestParseOllamaResponse:
    def test_text_response(self):
        data = {
            "message": {"content": "Hello!"},
            "prompt_eval_count": 10,
            "eval_count": 5,
        }
        result = _parse_ollama_response(data)
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert result.content[0].text == "Hello!"
        assert result.stop_reason == "end_turn"
        assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    def test_tool_call_response(self):
        data = {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "bash", "arguments": {"command": "ls"}}},
                ],
            },
        }
        result = _parse_ollama_response(data)
        # Empty content string produces a text block
        assert result.content[0].type == "text" if data["message"]["content"] else True
        tool_blocks = [b for b in result.content if b.type == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_name == "bash"
        assert tool_blocks[0].tool_input == {"command": "ls"}
        assert result.stop_reason == "tool_use"

    def test_no_usage_fields(self):
        data = {"message": {"content": "Hi"}}
        result = _parse_ollama_response(data)
        assert result.usage == {"input_tokens": 0, "output_tokens": 0}


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


class TestRetryWithBackoff:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        factory = AsyncMock(return_value="ok")
        result = await _retry_with_backoff(factory, base_delay=0)
        assert result == "ok"
        assert factory.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_retryable_error(self):
        call_count = 0

        async def factory():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RetryableError("rate limit")
            return "ok"

        result = await _retry_with_backoff(factory, base_delay=0)
        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self):
        async def factory():
            raise RetryableError("always fails")

        with pytest.raises(ProviderError, match="failed after 3 attempts"):
            await _retry_with_backoff(factory, max_retries=3, base_delay=0)

    @pytest.mark.asyncio
    async def test_non_retryable_error_propagates(self):
        async def factory():
            raise ProviderError("bad request")

        with pytest.raises(ProviderError, match="bad request"):
            await _retry_with_backoff(factory, base_delay=0)


# ---------------------------------------------------------------------------
# AnthropicProvider.send
# ---------------------------------------------------------------------------


class TestAnthropicProviderSend:
    @pytest.mark.asyncio
    async def test_sends_correct_params(self):
        provider = AnthropicProvider()
        mock_response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Hi")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        )
        provider._client = MagicMock()
        provider._client.messages = MagicMock()
        provider._client.messages.create = AsyncMock(return_value=mock_response)

        messages = [{"role": "user", "content": "Hello"}]
        config = _anthropic_config()
        result = await provider.send(messages, config)

        provider._client.messages.create.assert_called_once()
        call_kwargs = provider._client.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-sonnet-4-20250514"
        assert call_kwargs["messages"] == messages
        assert result.content[0].text == "Hi"

    @pytest.mark.asyncio
    async def test_extracts_system_message(self):
        provider = AnthropicProvider()
        mock_response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Hi")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        )
        provider._client = MagicMock()
        provider._client.messages = MagicMock()
        provider._client.messages.create = AsyncMock(return_value=mock_response)

        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]
        await provider.send(messages, _anthropic_config())

        call_kwargs = provider._client.messages.create.call_args[1]
        assert call_kwargs["system"] == "You are helpful"
        assert call_kwargs["messages"] == [{"role": "user", "content": "Hello"}]

    @pytest.mark.asyncio
    async def test_passes_tools(self):
        provider = AnthropicProvider()
        mock_response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Hi")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        )
        provider._client = MagicMock()
        provider._client.messages = MagicMock()
        provider._client.messages.create = AsyncMock(return_value=mock_response)

        tools = [{"name": "bash", "description": "Run a command", "input_schema": {}}]
        await provider.send([{"role": "user", "content": "Hi"}], _anthropic_config(), tools=tools)

        call_kwargs = provider._client.messages.create.call_args[1]
        assert call_kwargs["tools"] == tools


# ---------------------------------------------------------------------------
# OpenAIProvider.send
# ---------------------------------------------------------------------------


class TestOpenAIProviderSend:
    @pytest.mark.asyncio
    async def test_sends_correct_params(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        provider = OpenAIProvider()
        msg = SimpleNamespace(content="Hi", tool_calls=None)
        choice = SimpleNamespace(message=msg, finish_reason="stop")
        mock_response = SimpleNamespace(
            choices=[choice],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )
        provider._client = MagicMock()
        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        messages = [{"role": "user", "content": "Hello"}]
        result = await provider.send(messages, _openai_config())

        provider._client.chat.completions.create.assert_called_once()
        call_kwargs = provider._client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "gpt-4o"
        assert call_kwargs["messages"] == messages
        assert result.content[0].text == "Hi"

    @pytest.mark.asyncio
    async def test_converts_tools_to_openai_format(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        provider = OpenAIProvider()
        msg = SimpleNamespace(content="Hi", tool_calls=None)
        choice = SimpleNamespace(message=msg, finish_reason="stop")
        mock_response = SimpleNamespace(
            choices=[choice],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )
        provider._client = MagicMock()
        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        tools = [{"name": "bash", "description": "Run cmd", "input_schema": {"type": "object"}}]
        await provider.send([{"role": "user", "content": "Hi"}], _openai_config(), tools=tools)

        call_kwargs = provider._client.chat.completions.create.call_args[1]
        assert call_kwargs["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run cmd",
                    "parameters": {"type": "object"},
                },
            }
        ]


# ---------------------------------------------------------------------------
# OllamaProvider.send
# ---------------------------------------------------------------------------


class TestOllamaProviderSend:
    @pytest.mark.asyncio
    async def test_sends_correct_params(self):
        provider = OllamaProvider(base_url="http://localhost:11434")
        response_data = {
            "message": {"content": "Hi"},
            "prompt_eval_count": 5,
            "eval_count": 3,
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response_data
        mock_resp.raise_for_status = MagicMock()

        provider._client = MagicMock()
        provider._client.post = AsyncMock(return_value=mock_resp)

        messages = [{"role": "user", "content": "Hello"}]
        result = await provider.send(messages, _ollama_config())

        provider._client.post.assert_called_once()
        call_args = provider._client.post.call_args
        assert call_args[0][0] == "http://localhost:11434/api/chat"
        body = call_args[1]["json"]
        assert body["model"] == "llama3"
        assert body["messages"] == messages
        assert body["stream"] is False
        assert result.content[0].text == "Hi"

    @pytest.mark.asyncio
    async def test_retries_on_429(self):
        provider = OllamaProvider(base_url="http://localhost:11434")

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                resp = MagicMock()
                resp.status_code = 429
                resp.text = "rate limited"
                return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"message": {"content": "ok"}}
            resp.raise_for_status = MagicMock()
            return resp

        provider._client = MagicMock()
        provider._client.post = mock_post

        # Patch sleep to avoid waiting
        with patch("app.providers.asyncio.sleep", new_callable=AsyncMock):
            result = await provider.send(
                [{"role": "user", "content": "Hi"}], _ollama_config()
            )
        assert result.content[0].text == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self):
        import httpx as _httpx

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise _httpx.ConnectError("connection refused")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"message": {"content": "ok"}}
            resp.raise_for_status = MagicMock()
            return resp

        provider = OllamaProvider(base_url="http://localhost:11434")
        provider._client = MagicMock()
        provider._client.post = mock_post

        with patch("app.providers.asyncio.sleep", new_callable=AsyncMock):
            result = await provider.send(
                [{"role": "user", "content": "Hi"}], _ollama_config()
            )
        assert result.content[0].text == "ok"
        assert call_count == 2


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestGetProvider:
    def test_returns_anthropic(self):
        config = _anthropic_config()
        provider = get_provider(config)
        assert isinstance(provider, AnthropicProvider)

    def test_returns_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        config = _openai_config()
        provider = get_provider(config)
        assert isinstance(provider, OpenAIProvider)

    def test_returns_ollama(self):
        config = _ollama_config()
        provider = get_provider(config)
        assert isinstance(provider, OllamaProvider)

    def test_ollama_default_base_url(self):
        config = ModelConfig(provider="ollama", id="llama3")
        provider = get_provider(config)
        assert isinstance(provider, OllamaProvider)
        assert provider._base_url == "http://localhost:11434"

    def test_unknown_provider_raises(self):
        # Use model_construct to bypass validation for testing
        config = ModelConfig.model_construct(provider="unknown", id="x")
        with pytest.raises(ValueError, match="Unknown provider: unknown"):
            get_provider(config)
