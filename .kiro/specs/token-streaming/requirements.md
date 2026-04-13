# Requirements Document

## Introduction

Token streaming adds incremental text delivery to the Linchpin runtime. Currently, the orchestrator calls model providers with `stream=false`, waits for the complete response, and emits a single `agent.message` event with the full content. For local Ollama inference on CPU, this means 30–100+ seconds of silence before the user sees anything.

This feature introduces a new `agent.message_delta` event type that carries partial text chunks as they arrive from the model provider. The orchestrator streams from each provider's streaming API (Ollama `stream:true`, Anthropic streaming, OpenAI streaming), emits delta events in real time, and still emits the final `agent.message` event with the full accumulated content for replay and history. The console renders delta events by accumulating them into a growing message bubble, giving users immediate feedback during inference.

## Glossary

- **Linchpin_API**: The primary FastAPI service that exposes the HTTP surface, SSE/stream endpoint, orchestrator loop, and model provider adapters.
- **Orchestrator**: An async task within Linchpin_API that drives the agent loop for a single session.
- **Model_Provider**: An adapter within Linchpin_API that communicates with an external LLM API (Anthropic, OpenAI, or Ollama).
- **Delta_Event**: An event of type `agent.message_delta` carrying a partial text chunk from an in-progress model response.
- **SSE_Stream**: A Server-Sent Events connection that delivers real-time session events to clients.
- **EventStreamPanel**: The React component in linchpin-console that renders the session event stream.
- **Streaming_Response**: An async iterator yielded by a model provider adapter that produces incremental content chunks before the final complete response.
- **Accumulated_Content**: The full text assembled by concatenating all delta chunks for a single model turn, used to emit the final `agent.message` event.

## Requirements

### Requirement 1: Streaming Model Provider Interface

**User Story:** As a developer, I want model provider adapters to support streaming responses, so that tokens can be delivered incrementally instead of waiting for the full response.

#### Acceptance Criteria

1. THE Model_Provider protocol SHALL define a `send_streaming` method that returns a Streaming_Response async iterator yielding incremental content chunks.
2. WHEN the `send_streaming` method is called, THE Model_Provider SHALL open a streaming connection to the upstream LLM API.
3. WHEN a text chunk is received from the upstream LLM API, THE Streaming_Response SHALL yield a content chunk containing the partial text.
4. WHEN the upstream LLM streaming response completes, THE Streaming_Response SHALL yield a final chunk containing the stop reason and token usage statistics.
5. IF the upstream LLM API returns a tool_use block during streaming, THE Streaming_Response SHALL yield a complete tool_use content block after all tool_use fragments have been assembled.
6. IF a retryable error occurs during streaming, THE Model_Provider SHALL retry the request with exponential backoff up to 3 attempts, restarting the stream from the beginning.
7. IF a non-retryable error occurs during streaming, THE Model_Provider SHALL raise a ProviderError.

### Requirement 2: Anthropic Streaming Adapter

**User Story:** As a developer, I want the Anthropic adapter to stream tokens from the Anthropic Messages API, so that Anthropic-powered agents deliver incremental responses.

#### Acceptance Criteria

1. WHEN `send_streaming` is called on the Anthropic adapter, THE Anthropic adapter SHALL use the Anthropic SDK streaming interface to receive server-sent events from the Messages API.
2. WHEN a `content_block_delta` event with type `text_delta` is received, THE Anthropic adapter SHALL yield a text content chunk containing the delta text.
3. WHEN a `message_stop` event is received, THE Anthropic adapter SHALL yield a final chunk containing the stop reason and usage from the `message_delta` event.
4. WHEN a `content_block_start` event with type `tool_use` is received followed by `input_json_delta` events, THE Anthropic adapter SHALL accumulate the tool input JSON and yield a complete tool_use content block on `content_block_stop`.

### Requirement 3: OpenAI Streaming Adapter

**User Story:** As a developer, I want the OpenAI adapter to stream tokens from the OpenAI Chat Completions API, so that OpenAI-powered agents deliver incremental responses.

#### Acceptance Criteria

1. WHEN `send_streaming` is called on the OpenAI adapter, THE OpenAI adapter SHALL use the OpenAI SDK streaming interface with `stream=True` to receive chunked responses.
2. WHEN a chunk with a `delta.content` field is received, THE OpenAI adapter SHALL yield a text content chunk containing the delta content.
3. WHEN the stream completes with a `finish_reason`, THE OpenAI adapter SHALL yield a final chunk containing the stop reason and usage statistics.
4. WHEN a chunk with `delta.tool_calls` is received, THE OpenAI adapter SHALL accumulate tool call fragments and yield a complete tool_use content block when the tool call is fully assembled.

### Requirement 4: Ollama Streaming Adapter

**User Story:** As a developer, I want the Ollama adapter to stream tokens from the Ollama API, so that local model inference provides immediate feedback instead of 30–100+ second waits.

#### Acceptance Criteria

1. WHEN `send_streaming` is called on the Ollama adapter, THE Ollama adapter SHALL send a request to the Ollama `/api/chat` endpoint with `stream` set to `true`.
2. WHEN a newline-delimited JSON chunk with a `message.content` field is received, THE Ollama adapter SHALL yield a text content chunk containing the content fragment.
3. WHEN a chunk with `done` set to `true` is received, THE Ollama adapter SHALL yield a final chunk containing the stop reason and usage statistics extracted from `prompt_eval_count` and `eval_count`.
4. WHEN the Ollama response includes tool calls in the final chunk, THE Ollama adapter SHALL yield a complete tool_use content block for each tool call.

### Requirement 5: Delta Event Type

**User Story:** As a developer, I want a new event type for incremental text chunks, so that the system can distinguish between partial and complete messages.

#### Acceptance Criteria

1. THE Linchpin_API SHALL add `agent.message_delta` to the valid event type taxonomy.
2. THE `agent.message_delta` event payload SHALL contain a `delta` field with the partial text string.
3. THE `agent.message_delta` event payload SHALL contain a `message_id` field that groups all deltas belonging to the same model response.
4. THE Linchpin_API SHALL accept `agent.message_delta` as a valid event type in all event validation, storage, and retrieval operations.

### Requirement 6: Orchestrator Streaming Integration

**User Story:** As a developer, I want the orchestrator to use streaming when calling model providers, so that delta events are emitted as tokens arrive.

#### Acceptance Criteria

1. WHEN the Orchestrator sends a request to the Model_Provider, THE Orchestrator SHALL use the `send_streaming` method instead of the `send` method.
2. WHEN the Streaming_Response yields a text content chunk, THE Orchestrator SHALL emit an `agent.message_delta` event with the partial text in the `delta` field and a consistent `message_id`.
3. WHEN the Streaming_Response completes with accumulated text content, THE Orchestrator SHALL emit a final `agent.message` event containing the full Accumulated_Content.
4. WHEN the Streaming_Response yields a tool_use content block, THE Orchestrator SHALL process the tool call using the existing tool dispatch logic.
5. WHEN the Streaming_Response yields a thinking content block, THE Orchestrator SHALL emit an `agent.thinking` event.
6. THE Orchestrator SHALL update session token usage statistics from the final chunk's usage data.

### Requirement 7: Delta Event Persistence

**User Story:** As a developer, I want delta events stored in the event log, so that the full streaming history is available for replay and debugging.

#### Acceptance Criteria

1. WHEN an `agent.message_delta` event is emitted, THE Linchpin_API SHALL append the delta event to the session's event log with a unique cursor and monotonic sequence number.
2. WHEN events are retrieved via the REST pagination endpoint, THE Linchpin_API SHALL include `agent.message_delta` events in the response.
3. WHEN events are delivered via the SSE stream, THE SSE_Stream SHALL deliver `agent.message_delta` events in real time.

### Requirement 8: Console Delta Rendering

**User Story:** As a developer, I want the console to render streaming tokens as they arrive, so that I see the agent's response appearing incrementally instead of waiting for the full message.

#### Acceptance Criteria

1. WHEN an `agent.message_delta` event is received, THE EventStreamPanel SHALL append the delta text to a growing message bubble identified by the `message_id`.
2. WHEN multiple `agent.message_delta` events share the same `message_id`, THE EventStreamPanel SHALL concatenate the delta texts in sequence order to form the in-progress message.
3. WHEN an `agent.message` event is received after a sequence of delta events with the same `message_id`, THE EventStreamPanel SHALL replace the accumulated delta content with the final message content.
4. WHILE delta events are being received for an active `message_id`, THE EventStreamPanel SHALL display a visual streaming indicator on the in-progress message bubble.
5. WHEN the final `agent.message` event is received, THE EventStreamPanel SHALL remove the streaming indicator.

### Requirement 9: Backward Compatibility

**User Story:** As a developer, I want the streaming feature to be backward compatible, so that existing non-streaming behavior continues to work for replay and history.

#### Acceptance Criteria

1. THE Orchestrator SHALL continue to emit a final `agent.message` event with the full content after streaming completes, preserving the existing event log contract.
2. WHEN replaying a session event log that contains `agent.message_delta` events, THE EventStreamPanel SHALL render the final `agent.message` event as the authoritative content and skip rendering intermediate delta events.
3. WHEN a session event log contains `agent.message` events without preceding `agent.message_delta` events, THE EventStreamPanel SHALL render the message normally without any streaming behavior.
4. THE existing `send` method on each Model_Provider SHALL remain available and functional for non-streaming use cases.

### Requirement 10: Streaming Error Handling

**User Story:** As a developer, I want streaming errors to be handled gracefully, so that a mid-stream failure does not leave the session in a broken state.

#### Acceptance Criteria

1. IF the streaming connection to the upstream LLM API is interrupted mid-stream after delta events have been emitted, THEN THE Orchestrator SHALL emit a `session.error` event with a descriptive error message.
2. IF a streaming error occurs before any delta events have been emitted, THEN THE Model_Provider SHALL retry the request following the standard retry policy.
3. IF a streaming error occurs and the Orchestrator has emitted delta events, THEN THE Orchestrator SHALL NOT emit a final `agent.message` event with incomplete content.
4. IF a streaming error causes the session to fail, THEN THE Orchestrator SHALL transition the session status to failed.
