"""Unit tests for model provider adapters (OpenRouter + Ollama).

Covers:
- Each provider constructs correct API calls
- Responses are parsed into ModelResponse correctly
- Retry logic works (retries on retryable errors, gives up after 3)
- Factory returns correct provider
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import ModelConfig
from app.providers import (
    ContentBlock,
    ModelResponse,
    OllamaProvider,
    OpenRouterProvider,
    OPENROUTER_DEFAULT_BASE_URL,
    ProviderError,
    RetryableError,
    _parse_ollama_response,
    _parse_openrouter_response,
    _retry_with_backoff,
    get_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _openrouter_config(model_id: str = "anthropic/claude-sonnet-4") -> ModelConfig:
    return ModelConfig(provider="openrouter", id=model_id)


def _ollama_config() -> ModelConfig:
    return ModelConfig(provider="ollama", id="llama3", base_url="http://localhost:11434")


def _mock_post_response(status_code: int = 200, json_data: dict | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    resp.text = ""
    return resp


# ---------------------------------------------------------------------------
# Response parsing — OpenRouter
# ---------------------------------------------------------------------------


class TestParseOpenRouterResponse:
    def test_text_response(self):
        data = {
            "choices": [
                {"message": {"content": "Hello!"}, "finish_reason": "stop"},
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = _parse_openrouter_response(data)
        assert len(result.content) == 1
        assert result.content[0].type == "text"
        assert result.content[0].text == "Hello!"
        assert result.stop_reason == "end_turn"
        assert result.usage == {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

    def test_tool_call_response(self):
        data = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "bash",
                                    "arguments": '{"command": "ls"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15},
        }
        result = _parse_openrouter_response(data)
        assert len(result.content) == 1
        block = result.content[0]
        assert block.type == "tool_use"
        assert block.tool_use_id == "call_1"
        assert block.tool_name == "bash"
        assert block.tool_input == {"command": "ls"}
        assert result.stop_reason == "tool_use"

    def test_tool_call_with_non_string_arguments(self):
        data = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c",
                                "function": {"name": "read", "arguments": {"path": "/x"}},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        result = _parse_openrouter_response(data)
        assert result.content[0].tool_input == {"path": "/x"}

    def test_no_usage(self):
        data = {"choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}]}
        result = _parse_openrouter_response(data)
        assert result.usage == {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

    def test_no_choices(self):
        result = _parse_openrouter_response({})
        assert result.content == []

    # v0.2.0 item #10 — cache-token surfacing

    def test_cache_read_tokens_from_prompt_tokens_details(self):
        """OpenRouter / Anthropic puts cache-read tokens under
        ``prompt_tokens_details.cached_tokens``."""
        data = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 75},
            },
        }
        result = _parse_openrouter_response(data)
        assert result.usage == {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 75,
        }

    def test_cache_creation_tokens_top_level(self):
        """Anthropic-via-OpenRouter reports cache_creation_input_tokens at the
        top level of the usage object."""
        data = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 80,
                "completion_tokens": 10,
                "cache_creation_input_tokens": 200,
            },
        }
        result = _parse_openrouter_response(data)
        assert result.usage["cache_creation_input_tokens"] == 200
        assert result.usage["cache_read_input_tokens"] == 0

    def test_cache_tokens_both_present(self):
        data = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cache_creation_input_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 30},
            },
        }
        result = _parse_openrouter_response(data)
        assert result.usage["cache_creation_input_tokens"] == 50
        assert result.usage["cache_read_input_tokens"] == 30


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
        assert result.usage == {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

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
        tool_blocks = [b for b in result.content if b.type == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_name == "bash"
        assert tool_blocks[0].tool_input == {"command": "ls"}
        assert result.stop_reason == "tool_use"

    def test_no_usage_fields(self):
        data = {"message": {"content": "Hi"}}
        result = _parse_ollama_response(data)
        assert result.usage == {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


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
# OpenRouterProvider.send
# ---------------------------------------------------------------------------


class TestOpenRouterProviderSend:
    @pytest.mark.asyncio
    async def test_sends_correct_params(self):
        provider = OpenRouterProvider()
        response_data = {
            "choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
        mock_resp = _mock_post_response(200, response_data)

        provider._client = MagicMock()
        provider._client.post = AsyncMock(return_value=mock_resp)

        messages = [{"role": "user", "content": "Hello"}]
        config = _openrouter_config()
        result = await provider.send(messages, config, api_key="sk-test")

        provider._client.post.assert_called_once()
        call_args = provider._client.post.call_args
        assert call_args[0][0] == f"{OPENROUTER_DEFAULT_BASE_URL}/chat/completions"
        body = call_args[1]["json"]
        assert body["model"] == "anthropic/claude-sonnet-4"
        assert body["messages"] == messages
        # Streaming flag should not be set for send()
        assert "stream" not in body
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer sk-test"
        assert headers["Content-Type"] == "application/json"
        assert result.content[0].text == "Hi"
        assert result.usage == {"input_tokens": 5, "output_tokens": 3, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

    @pytest.mark.asyncio
    async def test_omits_authorization_without_api_key(self):
        provider = OpenRouterProvider()
        mock_resp = _mock_post_response(
            200,
            {"choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}]},
        )
        provider._client = MagicMock()
        provider._client.post = AsyncMock(return_value=mock_resp)

        await provider.send([{"role": "user", "content": "x"}], _openrouter_config())

        headers = provider._client.post.call_args[1]["headers"]
        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_converts_tools_to_openai_format(self):
        provider = OpenRouterProvider()
        mock_resp = _mock_post_response(
            200,
            {"choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}]},
        )
        provider._client = MagicMock()
        provider._client.post = AsyncMock(return_value=mock_resp)

        tools = [
            {"name": "bash", "description": "Run cmd", "input_schema": {"type": "object"}}
        ]
        await provider.send(
            [{"role": "user", "content": "Hi"}],
            _openrouter_config(),
            tools=tools,
            api_key="sk-test",
        )

        body = provider._client.post.call_args[1]["json"]
        assert body["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run cmd",
                    "parameters": {"type": "object"},
                },
            }
        ]

    @pytest.mark.asyncio
    async def test_retries_on_429(self):
        provider = OpenRouterProvider()

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                resp = MagicMock()
                resp.status_code = 429
                resp.text = "rate limited"
                return resp
            return _mock_post_response(
                200,
                {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
            )

        provider._client = MagicMock()
        provider._client.post = mock_post

        with patch("app.providers.asyncio.sleep", new_callable=AsyncMock):
            result = await provider.send(
                [{"role": "user", "content": "Hi"}],
                _openrouter_config(),
                api_key="sk-test",
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
            return _mock_post_response(
                200,
                {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
            )

        provider = OpenRouterProvider()
        provider._client = MagicMock()
        provider._client.post = mock_post

        with patch("app.providers.asyncio.sleep", new_callable=AsyncMock):
            result = await provider.send(
                [{"role": "user", "content": "Hi"}],
                _openrouter_config(),
                api_key="sk-test",
            )
        assert result.content[0].text == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_404_surfaces_response_body_message(self):
        """Non-retryable HTTP errors must include OpenRouter's error message."""
        provider = OpenRouterProvider()

        resp = MagicMock()
        resp.status_code = 404
        resp.text = '{"error":{"message":"No endpoints found for foo/bar","code":404}}'
        resp.reason_phrase = "Not Found"
        resp.json.return_value = {
            "error": {"message": "No endpoints found for foo/bar", "code": 404}
        }
        provider._client = MagicMock()
        provider._client.post = AsyncMock(return_value=resp)

        with pytest.raises(ProviderError, match="No endpoints found for foo/bar"):
            await provider.send(
                [{"role": "user", "content": "Hi"}],
                _openrouter_config("foo/bar"),
                api_key="sk-test",
            )

    @pytest.mark.asyncio
    async def test_400_with_non_json_body_falls_back_to_text(self):
        """Errors with non-JSON bodies should still surface useful detail."""
        provider = OpenRouterProvider()

        resp = MagicMock()
        resp.status_code = 400
        resp.text = "Bad Request: bogus model"
        resp.reason_phrase = "Bad Request"
        resp.json.side_effect = ValueError("not json")
        provider._client = MagicMock()
        provider._client.post = AsyncMock(return_value=resp)

        with pytest.raises(ProviderError, match="Bad Request: bogus model"):
            await provider.send(
                [{"role": "user", "content": "Hi"}],
                _openrouter_config(),
                api_key="sk-test",
            )


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
        mock_resp = _mock_post_response(200, response_data)

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
            return _mock_post_response(200, {"message": {"content": "ok"}})

        provider._client = MagicMock()
        provider._client.post = mock_post

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
            return _mock_post_response(200, {"message": {"content": "ok"}})

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
    def test_returns_openrouter(self):
        config = _openrouter_config()
        provider = get_provider(config)
        assert isinstance(provider, OpenRouterProvider)
        assert provider._base_url == OPENROUTER_DEFAULT_BASE_URL

    def test_openrouter_custom_base_url(self):
        config = ModelConfig(
            provider="openrouter",
            id="anthropic/claude-sonnet-4",
            base_url="https://example.com/v1",
        )
        provider = get_provider(config)
        assert isinstance(provider, OpenRouterProvider)
        assert provider._base_url == "https://example.com/v1"

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
        # Bypass pydantic validation to test the factory's own guard.
        config = ModelConfig.model_construct(provider="unknown", id="x")
        with pytest.raises(ValueError, match="Unknown provider: unknown"):
            get_provider(config)
