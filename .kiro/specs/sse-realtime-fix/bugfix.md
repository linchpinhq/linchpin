# Bugfix Requirements Document

## Introduction

Two related bugs prevent the Linchpin Console from delivering a working real-time chat experience. First, the SSE stream endpoint opens successfully but never delivers live events to the UI, forcing users to hard-refresh to see new agent responses. Second, on the 4th conversation turn the agent response bubble renders empty despite the orchestrator successfully completing the model call. Together these bugs make the console unusable for interactive sessions.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the SSE stream endpoint (`GET /v1/sessions/{id}/stream`) yields events via `EventSourceResponse`, THEN the system emits raw JSON strings without specifying an SSE `event` field or `data:` prefix, causing `@microsoft/fetch-event-source` to receive messages with `msg.data` as the raw JSON but potentially with the default event type, which may not trigger the `onmessage` callback depending on how `sse-starlette` serializes the generator output.

1.2 WHEN the SSE event generator yields a raw JSON string (e.g. `yield json.dumps({...})`), THEN `sse-starlette`'s `EventSourceResponse` wraps it as `data: <json>\n\n` but assigns no `event:` field, which works for `onmessage` — however the generator yields `dict`-serialized strings without the `seq` field, so the client-side `mergeEvents` function receives events missing the `seq` property needed for sorting.

1.3 WHEN the SSE stream replays existing events in Phase 1 and then enters Phase 2 (LISTEN/NOTIFY), THEN a race condition exists where the orchestrator's `notify()` call in `post_events` or `append_event` may fire before the SSE client's `listen()` subscription is active, causing the first live event after replay to be silently missed.

1.4 WHEN the `EventStreamPanel` SSE `useEffect` captures the `events` array at mount time to compute `latestCursor`, THEN the cursor is stale because the history fetch `useEffect` runs asynchronously and `events` is still `[]` when the SSE effect runs, causing the SSE connection to open with `cursor=undefined` and replay all events from the beginning (duplicating history) while still potentially missing the race window.

1.5 WHEN the orchestrator emits an `agent.message` event with payload `{"content": "..."}`, THEN the `EventStreamPanel` `renderEvent` function extracts text via `event.payload.text ?? event.payload.content ?? event.payload.message ?? ''`, which works for the `content` key — but on certain model turns (observed on the 4th turn), the orchestrator's `build_context` reads `payload.get("content", "")` confirming the key is `content`, yet the rendered bubble appears empty, suggesting the payload `content` field is an empty string when the model returns a response that the provider adapter maps to `block.text` but `block.text` is stored as `""` or the content block is of type `text` with an empty string.

1.6 WHEN the Ollama provider returns a response where the assistant message content is in a format that results in `block.text` being `None` or `""`, THEN the orchestrator emits `agent.message` with `{"content": ""}` and the console renders an empty bubble with no visible text.

### Expected Behavior (Correct)

2.1 WHEN the SSE stream endpoint yields events, THEN the system SHALL include the `seq` field in each SSE event JSON payload (i.e. `{"type": ..., "cursor": ..., "seq": ..., "payload": ...}`) so the client can properly sort and deduplicate events.

2.2 WHEN the SSE event generator yields events, THEN `sse-starlette`'s `EventSourceResponse` SHALL deliver them as properly formatted `data:` lines that `@microsoft/fetch-event-source`'s `onmessage` callback receives with the full JSON in `msg.data`.

2.3 WHEN the SSE stream transitions from Phase 1 (replay) to Phase 2 (live), THEN the system SHALL ensure no events are lost by re-querying for any events that arrived between the end of replay and the start of the LISTEN subscription, so that the NOTIFY race condition does not cause missed events.

2.4 WHEN the `EventStreamPanel` connects SSE, THEN it SHALL use the most recent cursor from the completed history fetch (not a stale closure value), so that SSE replay starts from the correct position without duplicating already-loaded events.

2.5 WHEN the orchestrator processes a model response text block, THEN the system SHALL ensure the `agent.message` event payload `content` field contains the actual model response text, and SHALL NOT emit an `agent.message` event with an empty `content` when the model did produce output.

2.6 WHEN the `EventStreamPanel` renders an `agent.message` event, THEN it SHALL display the content from the event payload, correctly handling both `text` and `content` keys, and SHALL NOT render an empty bubble when the payload contains non-empty content.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the `EventStreamPanel` loads historical events via `GET /v1/sessions/{id}/events`, THEN the system SHALL CONTINUE TO fetch and display them correctly on page load.

3.2 WHEN a session is in a terminal state (terminated, failed), THEN the `EventStreamPanel` SHALL CONTINUE TO not open an SSE connection and SHALL CONTINUE TO display only historical events.

3.3 WHEN the SSE client receives a malformed event, THEN the client SHALL CONTINUE TO log a warning and skip the event without crashing.

3.4 WHEN the user scrolls up in the event stream, THEN the auto-scroll behavior SHALL CONTINUE TO be suppressed until the user scrolls back to the bottom.

3.5 WHEN the `mergeEvents` function receives overlapping events from history and SSE, THEN it SHALL CONTINUE TO deduplicate by cursor and sort by `seq` ascending.

3.6 WHEN the orchestrator emits events for the first 3 conversation turns, THEN the system SHALL CONTINUE TO display agent responses with visible text content.

3.7 WHEN the user sends a `user.message` event via `POST /v1/sessions/{id}/events`, THEN the system SHALL CONTINUE TO append the event and trigger LISTEN/NOTIFY for the orchestrator.

3.8 WHEN the SSE connection is lost, THEN the SSE client SHALL CONTINUE TO auto-reconnect with the last received cursor for seamless replay.
