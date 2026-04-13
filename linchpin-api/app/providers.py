"""Model provider adapters for Anthropic, OpenAI, and Ollama.

Each adapter constructs provider-specific requests, parses responses into
a unified ``ModelResponse``, and retries retryable errors with exponential
backoff (3 attempts).

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

import anthropic
import httpx
import openai

from app.models import ModelConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Unified response types
# ---------------------------------------------------------------------------


@dataclass
class ContentBlock:
    """A single block in a model response."""

    type: str  # "text", "tool_use", "thinking"
    text: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None


@dataclass
class ModelResponse:
    """Unified response from any model provider."""

    content: list[ContentBlock] = field(default_factory=list)
    stop_reason: str | None = None
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})


@dataclass
class StreamChunk:
    """A single chunk from a streaming model response."""

    type: str  # "text_delta", "tool_use", "thinking", "final"
    text: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    stop_reason: str | None = None
    usage: dict | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Raised when a provider request fails after all retries."""


class RetryableError(Exception):
    """Raised for errors that should be retried."""


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_BASE_DELAY = 1.0  # seconds


async def _retry_with_backoff(coro_factory, *, max_retries: int = _MAX_RETRIES, base_delay: float = _BASE_DELAY):
    """Call *coro_factory()* up to *max_retries* times with exponential backoff.

    *coro_factory* is a zero-arg callable that returns a new awaitable each
    time (we cannot re-await the same coroutine).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await coro_factory()
        except RetryableError as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning("Retryable error (attempt %d/%d), retrying in %.1fs: %s", attempt + 1, max_retries, delay, exc)
                await asyncio.sleep(delay)
    raise ProviderError(f"Provider request failed after {max_retries} attempts: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ModelProviderProtocol(Protocol):
    """Interface that all model provider adapters must satisfy."""

    async def send(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None = None,
        api_key: str | None = None,
    ) -> ModelResponse: ...

    async def send_streaming(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[StreamChunk]: ...


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _is_anthropic_retryable(exc: Exception) -> bool:
    """Return True if the Anthropic SDK error is retryable."""
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code in (500, 502, 503):
        return True
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    return False


def _parse_anthropic_response(response) -> ModelResponse:
    """Convert an Anthropic Messages response to a unified ModelResponse."""
    blocks: list[ContentBlock] = []
    for block in response.content:
        if block.type == "text":
            blocks.append(ContentBlock(type="text", text=block.text))
        elif block.type == "tool_use":
            blocks.append(ContentBlock(
                type="tool_use",
                tool_use_id=block.id,
                tool_name=block.name,
                tool_input=block.input,
            ))
        elif block.type == "thinking":
            blocks.append(ContentBlock(type="thinking", text=block.thinking))

    return ModelResponse(
        content=blocks,
        stop_reason=response.stop_reason,
        usage={
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    )


class AnthropicProvider:
    """Adapter for the Anthropic Messages API."""

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic()

    def _build_kwargs(self, messages: list[dict], config: ModelConfig, tools: list[dict] | None) -> dict:
        """Build the kwargs dict shared by send() and send_streaming()."""
        kwargs: dict = {
            "model": config.id,
            "max_tokens": 4096,
            "messages": messages,
        }
        if messages and messages[0].get("role") == "system":
            kwargs["system"] = messages[0]["content"]
            kwargs["messages"] = messages[1:]
        if tools:
            kwargs["tools"] = tools
        return kwargs

    async def send(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None = None,
        api_key: str | None = None,
    ) -> ModelResponse:
        # Use explicit api_key if provided, otherwise fall back to SDK default (env var)
        client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else self._client
        kwargs = self._build_kwargs(messages, config, tools)

        async def _call():
            try:
                return _parse_anthropic_response(await client.messages.create(**kwargs))
            except Exception as exc:
                if _is_anthropic_retryable(exc):
                    raise RetryableError(str(exc)) from exc
                raise ProviderError(str(exc)) from exc

        return await _retry_with_backoff(_call)

    async def send_streaming(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else self._client
        kwargs = self._build_kwargs(messages, config, tools)

        async def _open_stream():
            try:
                return client.messages.stream(**kwargs)
            except Exception as exc:
                if _is_anthropic_retryable(exc):
                    raise RetryableError(str(exc)) from exc
                raise ProviderError(str(exc)) from exc

        stream_cm = await _retry_with_backoff(_open_stream)

        async with stream_cm as stream:
            current_tool_id: str | None = None
            current_tool_name: str | None = None
            tool_input_json = ""

            async for event in stream:
                if event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        current_tool_id = event.content_block.id
                        current_tool_name = event.content_block.name
                        tool_input_json = ""
                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield StreamChunk(type="text_delta", text=event.delta.text)
                    elif event.delta.type == "thinking_delta":
                        yield StreamChunk(type="thinking", text=event.delta.thinking)
                    elif event.delta.type == "input_json_delta":
                        tool_input_json += event.delta.partial_json
                elif event.type == "content_block_stop":
                    if current_tool_id:
                        yield StreamChunk(
                            type="tool_use",
                            tool_use_id=current_tool_id,
                            tool_name=current_tool_name,
                            tool_input=_json.loads(tool_input_json) if tool_input_json else {},
                        )
                        current_tool_id = None
                        current_tool_name = None
                        tool_input_json = ""
                elif event.type == "message_stop":
                    msg = stream.get_final_message()
                    yield StreamChunk(
                        type="final",
                        stop_reason=msg.stop_reason,
                        usage={
                            "input_tokens": msg.usage.input_tokens,
                            "output_tokens": msg.usage.output_tokens,
                        },
                    )


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def _parse_openai_response(response) -> ModelResponse:
    """Convert an OpenAI ChatCompletion response to a unified ModelResponse."""
    choice = response.choices[0]
    msg = choice.message
    blocks: list[ContentBlock] = []

    if msg.content:
        blocks.append(ContentBlock(type="text", text=msg.content))

    if msg.tool_calls:
        for tc in msg.tool_calls:
            import json
            tool_input = tc.function.arguments
            if isinstance(tool_input, str):
                try:
                    tool_input = json.loads(tool_input)
                except json.JSONDecodeError:
                    tool_input = {"raw": tool_input}
            blocks.append(ContentBlock(
                type="tool_use",
                tool_use_id=tc.id,
                tool_name=tc.function.name,
                tool_input=tool_input,
            ))

    stop_reason = "end_turn"
    if choice.finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif choice.finish_reason == "stop":
        stop_reason = "end_turn"

    usage_data = {"input_tokens": 0, "output_tokens": 0}
    if response.usage:
        usage_data = {
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
        }

    return ModelResponse(content=blocks, stop_reason=stop_reason, usage=usage_data)


def _is_openai_retryable(exc: Exception) -> bool:
    """Return True if the OpenAI SDK error is retryable."""
    if isinstance(exc, openai.RateLimitError):
        return True
    if isinstance(exc, openai.APIStatusError) and exc.status_code in (500, 502, 503):
        return True
    if isinstance(exc, openai.APIConnectionError):
        return True
    return False


class OpenAIProvider:
    """Adapter for the OpenAI Chat Completions API."""

    def __init__(self) -> None:
        self._client = openai.AsyncOpenAI()

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        """Convert Linchpin tool defs to OpenAI function-calling format."""
        openai_tools = []
        for t in tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            })
        return openai_tools

    async def send(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None = None,
        api_key: str | None = None,
    ) -> ModelResponse:
        client = openai.AsyncOpenAI(api_key=api_key) if api_key else self._client

        async def _call():
            try:
                kwargs: dict = {
                    "model": config.id,
                    "messages": messages,
                }
                if tools:
                    kwargs["tools"] = self._convert_tools(tools)
                return _parse_openai_response(await client.chat.completions.create(**kwargs))
            except Exception as exc:
                if _is_openai_retryable(exc):
                    raise RetryableError(str(exc)) from exc
                raise ProviderError(str(exc)) from exc

        return await _retry_with_backoff(_call)

    async def send_streaming(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        client = openai.AsyncOpenAI(api_key=api_key) if api_key else self._client

        async def _open_stream():
            try:
                kwargs: dict = {
                    "model": config.id,
                    "messages": messages,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if tools:
                    kwargs["tools"] = self._convert_tools(tools)
                return await client.chat.completions.create(**kwargs)
            except Exception as exc:
                if _is_openai_retryable(exc):
                    raise RetryableError(str(exc)) from exc
                raise ProviderError(str(exc)) from exc

        stream = await _retry_with_backoff(_open_stream)

        tool_calls: dict[int, dict] = {}  # index -> {id, name, args}
        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if choice and choice.delta and choice.delta.content:
                yield StreamChunk(type="text_delta", text=choice.delta.content)
            if choice and choice.delta and choice.delta.tool_calls:
                for tc in choice.delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {
                            "id": tc.id or "",
                            "name": (tc.function.name if tc.function else "") or "",
                            "args": "",
                        }
                    else:
                        if tc.id:
                            tool_calls[idx]["id"] = tc.id
                        if tc.function and tc.function.name:
                            tool_calls[idx]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls[idx]["args"] += tc.function.arguments
            if choice and choice.finish_reason:
                # Yield assembled tool calls
                for tc_data in tool_calls.values():
                    yield StreamChunk(
                        type="tool_use",
                        tool_use_id=tc_data["id"],
                        tool_name=tc_data["name"],
                        tool_input=_json.loads(tc_data["args"]) if tc_data["args"] else {},
                    )
                tool_calls.clear()
                usage = {"input_tokens": 0, "output_tokens": 0}
                if chunk.usage:
                    usage = {
                        "input_tokens": chunk.usage.prompt_tokens or 0,
                        "output_tokens": chunk.usage.completion_tokens or 0,
                    }
                stop_reason = "end_turn"
                if choice.finish_reason == "tool_calls":
                    stop_reason = "tool_use"
                yield StreamChunk(type="final", stop_reason=stop_reason, usage=usage)


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def _parse_ollama_response(data: dict) -> ModelResponse:
    """Convert an Ollama /api/chat JSON response to a unified ModelResponse."""
    msg = data.get("message", {})
    blocks: list[ContentBlock] = []

    if msg.get("content"):
        blocks.append(ContentBlock(type="text", text=msg["content"]))

    for tc in msg.get("tool_calls", []):
        func = tc.get("function", {})
        blocks.append(ContentBlock(
            type="tool_use",
            tool_use_id=None,
            tool_name=func.get("name"),
            tool_input=func.get("arguments", {}),
        ))

    stop_reason = "end_turn"
    if msg.get("tool_calls"):
        stop_reason = "tool_use"

    usage_data = {"input_tokens": 0, "output_tokens": 0}
    if "prompt_eval_count" in data:
        usage_data["input_tokens"] = data["prompt_eval_count"]
    if "eval_count" in data:
        usage_data["output_tokens"] = data["eval_count"]

    return ModelResponse(content=blocks, stop_reason=stop_reason, usage=usage_data)


class OllamaProvider:
    """Adapter for the Ollama REST API."""

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=120.0)

    async def send(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None = None,
        api_key: str | None = None,
    ) -> ModelResponse:
        async def _call():
            try:
                body: dict = {
                    "model": config.id,
                    "messages": messages,
                    "stream": False,
                }
                if tools:
                    body["tools"] = tools
                headers = {}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                resp = await self._client.post(
                    f"{self._base_url}/api/chat", json=body, headers=headers,
                )
                if resp.status_code == 429 or resp.status_code in (500, 502, 503):
                    raise RetryableError(f"Ollama returned {resp.status_code}: {resp.text}")
                resp.raise_for_status()
                return _parse_ollama_response(resp.json())
            except RetryableError:
                raise
            except httpx.ConnectError as exc:
                raise RetryableError(str(exc)) from exc
            except httpx.HTTPStatusError as exc:
                raise ProviderError(str(exc)) from exc
            except Exception as exc:
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError(str(exc)) from exc

        return await _retry_with_backoff(_call)

    async def send_streaming(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        body: dict = {
            "model": config.id,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        async def _open_stream():
            try:
                req = self._client.build_request(
                    "POST", f"{self._base_url}/api/chat", json=body, headers=headers,
                )
                resp = await self._client.send(req, stream=True)
                if resp.status_code == 429 or resp.status_code in (500, 502, 503):
                    await resp.aclose()
                    raise RetryableError(f"Ollama returned {resp.status_code}")
                resp.raise_for_status()
                return resp
            except RetryableError:
                raise
            except httpx.ConnectError as exc:
                raise RetryableError(str(exc)) from exc
            except httpx.HTTPStatusError as exc:
                raise ProviderError(str(exc)) from exc
            except Exception as exc:
                if isinstance(exc, (ProviderError, RetryableError)):
                    raise
                raise ProviderError(str(exc)) from exc

        resp = await _retry_with_backoff(_open_stream)

        try:
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                data = _json.loads(line)
                if data.get("done"):
                    # Yield tool_use chunks from the final message
                    for tc in data.get("message", {}).get("tool_calls", []):
                        func = tc.get("function", {})
                        yield StreamChunk(
                            type="tool_use",
                            tool_name=func.get("name"),
                            tool_input=func.get("arguments", {}),
                        )
                    stop_reason = "end_turn"
                    if data.get("message", {}).get("tool_calls"):
                        stop_reason = "tool_use"
                    usage = {
                        "input_tokens": data.get("prompt_eval_count", 0),
                        "output_tokens": data.get("eval_count", 0),
                    }
                    yield StreamChunk(type="final", stop_reason=stop_reason, usage=usage)
                else:
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield StreamChunk(type="text_delta", text=content)
        finally:
            await resp.aclose()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_provider(config: ModelConfig) -> ModelProviderProtocol:
    """Return the appropriate provider adapter for *config*."""
    if config.provider == "anthropic":
        return AnthropicProvider()
    if config.provider == "openai":
        return OpenAIProvider()
    if config.provider == "ollama":
        return OllamaProvider(base_url=config.base_url or "http://localhost:11434")
    raise ValueError(f"Unknown provider: {config.provider}")
