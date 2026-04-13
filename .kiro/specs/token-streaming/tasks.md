# Implementation Plan: Token Streaming

## Overview

Add incremental token delivery across all three model provider adapters (Anthropic, OpenAI, Ollama), wire streaming through the orchestrator with `agent.message_delta` events, and update the console to render deltas as a growing message bubble. The existing `send()` method and final `agent.message` event contract are preserved for backward compatibility.

## Tasks

- [x] 1. Add StreamChunk dataclass and update event type taxonomy
  - [x] 1.1 Add `StreamChunk` dataclass to `linchpin-api/app/providers.py`
    - Define dataclass with fields: `type`, `text`, `tool_use_id`, `tool_name`, `tool_input`, `stop_reason`, `usage`
    - Place it alongside the existing `ContentBlock` and `ModelResponse` dataclasses
    - _Requirements: 1.1_

  - [x] 1.2 Add `agent.message_delta` to `EVENT_TYPES` and `EventType` in `linchpin-api/app/models.py`
    - Add `"agent.message_delta"` to the `EVENT_TYPES` frozenset
    - Add `"agent.message_delta"` to the `EventType` Literal union
    - Verify `is_valid_event_type("agent.message_delta")` returns `True`
    - _Requirements: 5.1, 5.4_

  - [x] 1.3 Add `send_streaming` to `ModelProviderProtocol` in `linchpin-api/app/providers.py`
    - Add `send_streaming()` method signature returning `AsyncIterator[StreamChunk]`
    - Import `AsyncIterator` from `collections.abc`
    - _Requirements: 1.1, 1.2_

  - [ ]* 1.4 Write unit tests for StreamChunk and event type additions
    - Test `StreamChunk` instantiation with various field combinations
    - Test `is_valid_event_type("agent.message_delta")` returns `True`
    - Test `"agent.message_delta"` is in `EVENT_TYPES`
    - _Requirements: 5.1, 5.4_

- [x] 2. Implement Anthropic streaming adapter
  - [x] 2.1 Implement `send_streaming()` on `AnthropicProvider` in `linchpin-api/app/providers.py`
    - Use `client.messages.stream()` async context manager
    - Handle `content_block_start`, `content_block_delta`, `content_block_stop`, `message_stop` events
    - Yield `StreamChunk(type="text_delta")` for text deltas
    - Accumulate `input_json_delta` fragments and yield assembled `StreamChunk(type="tool_use")` on block stop
    - Yield `StreamChunk(type="final")` with stop_reason and usage on message_stop
    - Wrap stream setup in retry logic for retryable errors before chunks are yielded
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [ ]* 2.2 Write property test: text chunk mapping for Anthropic
    - **Property 1: Text chunk mapping preserves content**
    - **Validates: Requirements 1.3, 2.2**

  - [ ]* 2.3 Write property test: final chunk carries stop reason and usage for Anthropic
    - **Property 2: Final chunk carries stop reason and usage**
    - **Validates: Requirements 1.4, 2.3**

  - [ ]* 2.4 Write property test: tool_use fragment assembly for Anthropic
    - **Property 3: Tool_use fragment assembly round-trip**
    - **Validates: Requirements 1.5, 2.4**

  - [ ]* 2.5 Write unit tests for Anthropic `send_streaming()` with mocked SDK
    - Test text streaming, tool_use assembly, thinking chunks, final chunk
    - Test retry on retryable errors, ProviderError on non-retryable errors
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 1.6, 1.7_

- [x] 3. Implement OpenAI streaming adapter
  - [x] 3.1 Implement `send_streaming()` on `OpenAIProvider` in `linchpin-api/app/providers.py`
    - Use `client.chat.completions.create(stream=True, stream_options={"include_usage": True})`
    - Yield `StreamChunk(type="text_delta")` for `choice.delta.content`
    - Accumulate `delta.tool_calls` fragments by index, yield assembled `StreamChunk(type="tool_use")` on finish_reason
    - Yield `StreamChunk(type="final")` with mapped stop_reason and usage
    - Wrap stream setup in retry logic
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [ ]* 3.2 Write property test: text chunk mapping for OpenAI
    - **Property 1: Text chunk mapping preserves content**
    - **Validates: Requirements 1.3, 3.2**

  - [ ]* 3.3 Write property test: tool_use fragment assembly for OpenAI
    - **Property 3: Tool_use fragment assembly round-trip**
    - **Validates: Requirements 1.5, 3.4**

  - [ ]* 3.4 Write unit tests for OpenAI `send_streaming()` with mocked SDK
    - Test text streaming, tool_call assembly, final chunk with usage
    - Test retry and error handling
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 1.6, 1.7_

- [x] 4. Implement Ollama streaming adapter
  - [x] 4.1 Implement `send_streaming()` on `OllamaProvider` in `linchpin-api/app/providers.py`
    - Use `self._client.stream("POST", ...)` with `stream: true` in body
    - Read NDJSON lines via `resp.aiter_lines()`
    - Yield `StreamChunk(type="text_delta")` for each line with `message.content`
    - On `done: true`, yield tool_use chunks from `message.tool_calls` and `StreamChunk(type="final")` with usage from `prompt_eval_count`/`eval_count`
    - Wrap stream setup in retry logic
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [ ]* 4.2 Write property test: text chunk mapping for Ollama
    - **Property 1: Text chunk mapping preserves content**
    - **Validates: Requirements 1.3, 4.2**

  - [ ]* 4.3 Write unit tests for Ollama `send_streaming()` with mocked httpx
    - Test NDJSON text streaming, done chunk with usage, tool_calls in final message
    - Test retry on connection errors
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 1.6, 1.7_

- [x] 5. Checkpoint - Verify all provider streaming adapters
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Integrate streaming into the orchestrator
  - [x] 6.1 Modify `run_session()` in `linchpin-api/app/orchestrator.py` to use `send_streaming()`
    - Replace `provider.send()` call with `provider.send_streaming()` async iteration
    - Generate a `message_id` (UUID) at the start of each model turn
    - On `text_delta` chunks: accumulate text and emit `agent.message_delta` event with `{delta, message_id}`
    - On `thinking` chunks: emit `agent.thinking` event
    - On `tool_use` chunks: collect into `content_blocks` list
    - On `final` chunk: extract stop_reason and usage
    - After stream completes: emit `agent.message` with full accumulated content, update usage, process tool_use blocks through existing dispatch logic
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 5.2, 5.3, 9.1_

  - [x] 6.2 Add mid-stream error handling in the orchestrator
    - Wrap the stream iteration in try/except
    - If error occurs after delta events emitted: do NOT emit `agent.message`, emit `session.error` with `{error, source: "streaming"}`, transition to `failed`
    - If error occurs before any deltas: let existing ProviderError handling apply (retry at provider level)
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

  - [ ]* 6.3 Write property test: delta emission count matches chunk count
    - **Property 5: Orchestrator emits delta for every text chunk**
    - **Validates: Requirements 6.2**

  - [ ]* 6.4 Write property test: accumulated content equals chunk concatenation
    - **Property 6: Accumulated content equals chunk concatenation**
    - **Validates: Requirements 6.3, 9.1**

  - [ ]* 6.5 Write property test: usage statistics propagation
    - **Property 7: Usage statistics propagation**
    - **Validates: Requirements 6.6**

  - [ ]* 6.6 Write property test: no incomplete final message on mid-stream error
    - **Property 11: No incomplete final message on mid-stream error**
    - **Validates: Requirements 10.3**

  - [ ]* 6.7 Write property test: delta event payload contains required fields
    - **Property 4: Delta event payload contains required fields**
    - **Validates: Requirements 5.2, 5.3**

  - [ ]* 6.8 Write unit tests for orchestrator streaming integration
    - Test full streaming loop with mocked provider yielding text chunks
    - Test tool_use dispatch after streaming
    - Test mid-stream error handling (no agent.message emitted, session.error emitted, session transitions to failed)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 10.1, 10.3, 10.4_

- [x] 7. Checkpoint - Verify orchestrator streaming integration
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Update console to render streaming deltas
  - [x] 8.1 Add `agent.message_delta` to TypeScript `EventType` in `linchpin-console/src/types.ts`
    - Add `"agent.message_delta"` to the `EventType` union type
    - _Requirements: 5.1_

  - [x] 8.2 Update `EventStreamPanel` in `linchpin-console/src/components/EventStreamPanel.tsx` for delta accumulation
    - Add `streamingMessages` state: `Map<string, string>` keyed by `message_id`
    - On `agent.message_delta` events: accumulate delta text into the map by `message_id`
    - Render streaming bubbles from `streamingMessages` map with a visual streaming indicator (e.g. CSS class)
    - On `agent.message` event: clear the corresponding streaming message entry, render the final content
    - Add `agent.message_delta` to `eventTypeClass()` returning `'agent-message'`
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [x] 8.3 Handle replay mode in `EventStreamPanel`
    - During initial event load (historical replay), skip rendering `agent.message_delta` events as separate bubbles
    - Only render the final `agent.message` events for completed message sequences
    - Ensure backward compatibility: sessions without delta events render normally
    - _Requirements: 9.2, 9.3_

  - [ ]* 8.4 Write property test: console delta accumulation
    - **Property 8: Console delta accumulation**
    - **Validates: Requirements 8.1, 8.2**

  - [ ]* 8.5 Write property test: console final message replacement
    - **Property 9: Console final message replacement**
    - **Validates: Requirements 8.3**

  - [ ]* 8.6 Write property test: replay skips delta events
    - **Property 10: Replay skips delta events**
    - **Validates: Requirements 9.2**

  - [ ]* 8.7 Write unit tests for EventStreamPanel streaming behavior
    - Test delta accumulation renders growing bubble text
    - Test final message replaces accumulated deltas
    - Test streaming indicator appears during active streaming, disappears after final message
    - Test replay mode only renders final agent.message events
    - Test backward compatibility with non-streaming event logs
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 9.2, 9.3_

- [x] 9. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests use Hypothesis (backend) and fast-check (frontend)
- The existing `send()` method is preserved on all providers — no changes needed (Requirement 9.4)
- No database migration required — `agent.message_delta` uses the existing events table schema
- Delta events flow through existing SSE/polling infrastructure with no changes (Requirement 7.1, 7.2, 7.3)
