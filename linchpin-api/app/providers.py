"""Model provider adapters.

Linchpin supports two providers:

- ``openrouter`` — cloud aggregator that exposes ~200 models (Claude, GPT,
  Gemini, Llama, DeepSeek, Mistral, Qwen, …) behind a single
  OpenAI-compatible HTTP API.
- ``ollama`` — local inference via the Ollama REST API.

Each adapter constructs provider-specific requests, parses responses into
a unified ``ModelResponse``, and retries retryable errors with exponential
backoff (3 attempts).
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

import httpx

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


async def _retry_with_backoff(coro_factory, max_retries: int = 3, base_delay: float = 1.0):
    """Run ``coro_factory()`` with exponential-backoff retry on RetryableError."""
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await coro_factory()
        except RetryableError as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Retryable error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, max_retries, delay, exc,
                )
                await asyncio.sleep(delay)
    raise ProviderError(
        f"Provider request failed after {max_retries} attempts: {last_exc}"
    ) from last_exc


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
# OpenRouter
# ---------------------------------------------------------------------------

OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


def _extract_openrouter_error(resp: httpx.Response) -> str:
    """Return a human-readable error string from an OpenRouter error response.

    OpenRouter returns ``{"error": {"message": "...", "code": ...}}`` on
    failure. Falls back to the raw body if the JSON shape is unexpected.
    """
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        return resp.text or resp.reason_phrase
    except Exception:
        return resp.text or resp.reason_phrase


async def _read_stream_body(resp: httpx.Response) -> str:
    """Read the body of a streaming response that hasn't been consumed yet."""
    try:
        await resp.aread()
    except Exception:
        return resp.reason_phrase
    return _extract_openrouter_error(resp)


def _convert_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Linchpin tool definitions to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in tools
    ]


def _parse_openrouter_response(data: dict) -> ModelResponse:
    """Convert an OpenRouter /chat/completions JSON response to a ModelResponse.

    OpenRouter responses follow the OpenAI Chat Completions shape.
    """
    choices = data.get("choices") or []
    if not choices:
        return ModelResponse()

    choice = choices[0]
    msg = choice.get("message", {}) or {}
    blocks: list[ContentBlock] = []

    if msg.get("content"):
        blocks.append(ContentBlock(type="text", text=msg["content"]))

    for tc in msg.get("tool_calls") or []:
        func = tc.get("function", {}) or {}
        raw_args = func.get("arguments", "")
        if isinstance(raw_args, str):
            try:
                tool_input = _json.loads(raw_args) if raw_args else {}
            except _json.JSONDecodeError:
                tool_input = {"raw": raw_args}
        else:
            tool_input = raw_args or {}
        blocks.append(ContentBlock(
            type="tool_use",
            tool_use_id=tc.get("id"),
            tool_name=func.get("name"),
            tool_input=tool_input,
        ))

    finish = choice.get("finish_reason")
    stop_reason = "tool_use" if finish == "tool_calls" else "end_turn"

    usage_raw = data.get("usage") or {}
    usage = {
        "input_tokens": usage_raw.get("prompt_tokens", 0) or 0,
        "output_tokens": usage_raw.get("completion_tokens", 0) or 0,
    }

    return ModelResponse(content=blocks, stop_reason=stop_reason, usage=usage)


class OpenRouterProvider:
    """Adapter for the OpenRouter chat-completions API.

    OpenRouter speaks the OpenAI Chat Completions protocol, so requests and
    responses use the standard OAI shape. The provider sends raw HTTP via
    ``httpx`` rather than depending on the ``openai`` SDK.
    """

    def __init__(self, base_url: str = OPENROUTER_DEFAULT_BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=120.0)

    def _build_headers(self, api_key: str | None) -> dict:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers["HTTP-Referer"] = "https://github.com/flowagent-sh/linchpin"
        headers["X-Title"] = "Linchpin"
        return headers

    def _build_body(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None,
        stream: bool,
    ) -> dict:
        body: dict = {
            "model": config.id,
            "messages": messages,
        }
        if tools:
            body["tools"] = _convert_tools_to_openai(tools)
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    async def send(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None = None,
        api_key: str | None = None,
    ) -> ModelResponse:
        body = self._build_body(messages, config, tools, stream=False)
        headers = self._build_headers(api_key)

        async def _call():
            try:
                resp = await self._client.post(
                    f"{self._base_url}/chat/completions", json=body, headers=headers,
                )
                if resp.status_code in (429, 500, 502, 503):
                    raise RetryableError(
                        f"OpenRouter returned {resp.status_code}: "
                        f"{_extract_openrouter_error(resp)}"
                    )
                if resp.status_code >= 400:
                    raise ProviderError(
                        f"OpenRouter returned {resp.status_code}: "
                        f"{_extract_openrouter_error(resp)}"
                    )
                return _parse_openrouter_response(resp.json())
            except (RetryableError, ProviderError):
                raise
            except httpx.ConnectError as exc:
                raise RetryableError(str(exc)) from exc
            except Exception as exc:
                raise ProviderError(str(exc)) from exc

        return await _retry_with_backoff(_call)

    async def send_streaming(
        self,
        messages: list[dict],
        config: ModelConfig,
        tools: list[dict] | None = None,
        api_key: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        body = self._build_body(messages, config, tools, stream=True)
        headers = self._build_headers(api_key)

        async def _open_stream():
            try:
                req = self._client.build_request(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers=headers,
                )
                resp = await self._client.send(req, stream=True)
                if resp.status_code in (429, 500, 502, 503):
                    detail = await _read_stream_body(resp)
                    await resp.aclose()
                    raise RetryableError(
                        f"OpenRouter returned {resp.status_code}: {detail}"
                    )
                if resp.status_code >= 400:
                    detail = await _read_stream_body(resp)
                    await resp.aclose()
                    raise ProviderError(
                        f"OpenRouter returned {resp.status_code}: {detail}"
                    )
                return resp
            except (RetryableError, ProviderError):
                raise
            except httpx.ConnectError as exc:
                raise RetryableError(str(exc)) from exc
            except Exception as exc:
                raise ProviderError(str(exc)) from exc

        resp = await _retry_with_backoff(_open_stream)

        tool_calls: dict[int, dict] = {}
        usage: dict | None = None
        stop_reason = "end_turn"

        try:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = _json.loads(payload)
                except _json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage = {
                        "input_tokens": u.get("prompt_tokens", 0) or 0,
                        "output_tokens": u.get("completion_tokens", 0) or 0,
                    }

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}

                if delta.get("content"):
                    yield StreamChunk(type="text_delta", text=delta["content"])

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(
                        idx, {"id": "", "name": "", "args": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    func = tc.get("function") or {}
                    if func.get("name"):
                        slot["name"] = func["name"]
                    if func.get("arguments"):
                        slot["args"] += func["arguments"]

                finish = choice.get("finish_reason")
                if finish:
                    if finish == "tool_calls":
                        stop_reason = "tool_use"
                    elif finish == "stop":
                        stop_reason = "end_turn"
                    else:
                        stop_reason = finish

                    for slot in tool_calls.values():
                        try:
                            tool_input = _json.loads(slot["args"]) if slot["args"] else {}
                        except _json.JSONDecodeError:
                            tool_input = {"raw": slot["args"]}
                        yield StreamChunk(
                            type="tool_use",
                            tool_use_id=slot["id"],
                            tool_name=slot["name"],
                            tool_input=tool_input,
                        )
                    tool_calls.clear()
                    yield StreamChunk(
                        type="final",
                        stop_reason=stop_reason,
                        usage=usage or {"input_tokens": 0, "output_tokens": 0},
                    )
        finally:
            await resp.aclose()


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
    if config.provider == "openrouter":
        return OpenRouterProvider(base_url=config.base_url or OPENROUTER_DEFAULT_BASE_URL)
    if config.provider == "ollama":
        return OllamaProvider(base_url=config.base_url or "http://localhost:11434")
    raise ValueError(f"Unknown provider: {config.provider}")
