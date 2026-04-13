# Implementation Plan: Linchpin MVP

## Overview

Two Python 3.12 services (linchpin-api via FastAPI, linchpin-connector) plus Postgres 16, deployed via docker-compose. Implementation follows a four-week build plan: skeleton and sandbox → orchestrator and events → permissions and connector → polish and ship. All code in Python, tests via pytest + Hypothesis.

## Tasks

- [x] 1. Project scaffolding and database setup
  - [x] 1.1 Create project structure with pyproject.toml, dependencies, and entry points
    - Create `linchpin-api/` and `linchpin-connector/` directories
    - Add `pyproject.toml` for each with dependencies: fastapi, uvicorn, asyncpg, pydantic, docker, httpx, anthropic, openai, hypothesis, pytest, alembic
    - Create `linchpin-api/app/main.py` with FastAPI app skeleton and lifespan handler
    - Create `linchpin-connector/app/main.py` with FastAPI app skeleton
    - _Requirements: 21.1, 22.1_

  - [x] 1.2 Set up Alembic migrations and database schema
    - Initialize Alembic in `linchpin-api/alembic/`
    - Create initial migration with agents, environments, sessions, events tables per design schema
    - Add startup migration check in lifespan handler
    - _Requirements: 21.1, 21.2, 21.3_

  - [x] 1.3 Implement Pydantic data models
    - Create `linchpin-api/app/models.py` with Agent, ModelConfig, ToolConfig, MCPServerConfig, Environment, EnvironmentConfig, NetworkingConfig, Session, SessionStats, SessionUsage, Event models
    - Create request/response models for all CRUD endpoints
    - Implement event type taxonomy as a validated literal set
    - _Requirements: 1.1, 3.1, 5.1, 10.1, 10.2, 10.3_

  - [ ]* 1.4 Write property test for event type validation
    - **Property 9: Event type validation**
    - Generate random strings, verify accept/reject matches the valid event type set exactly
    - **Validates: Requirements 10.1, 10.2, 10.3, 10.4**

  - [x] 1.5 Implement database connection pool and helpers
    - Create `linchpin-api/app/db.py` with asyncpg pool creation, query helpers, LISTEN/NOTIFY utilities
    - Wire pool into FastAPI lifespan (create on startup, close on shutdown)
    - _Requirements: 21.2_

- [x] 2. Authentication and agent/environment CRUD
  - [x] 2.1 Implement auth middleware
    - Create `linchpin-api/app/auth.py` with bearer token validation against `LINCHPIN_API_KEY` env var
    - Add as FastAPI dependency for all `/v1/` routes
    - Return 401 with `{"error": "unauthorized", "message": "..."}` for missing/invalid tokens
    - _Requirements: 18.1, 18.2, 18.3_

  - [ ]* 2.2 Write property test for authentication token validation
    - **Property 11: Authentication token validation**
    - Generate random tokens, verify accept iff matches configured key; missing/malformed → 401
    - **Validates: Requirements 18.1, 18.2, 18.3**

  - [x] 2.3 Implement agent CRUD endpoints
    - Create `linchpin-api/app/routes/agents.py`
    - POST `/v1/agents` — validate payload, insert into DB, return 201 with full Agent resource
    - GET `/v1/agents/{id}` — fetch by id, return 404 if not found
    - GET `/v1/agents` — paginated list
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3_

  - [ ]* 2.4 Write property tests for agent round-trip and validation
    - **Property 1: Agent round-trip** — Generate random valid agent payloads, create + retrieve, verify equivalence
    - **Property 2: Agent validation rejects invalid input** — Generate random invalid payloads (bad provider, bad permission, missing fields), verify 422 rejection
    - **Validates: Requirements 1.1, 1.2, 1.4, 1.5, 1.6, 2.1**

  - [x] 2.5 Implement environment CRUD endpoints
    - Create `linchpin-api/app/routes/environments.py`
    - POST `/v1/environments` — validate payload, insert into DB, return 201
    - GET `/v1/environments/{id}` — fetch by id, return 404 if not found
    - GET `/v1/environments` — paginated list
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3_

  - [ ]* 2.6 Write property tests for environment round-trip and validation
    - **Property 3: Environment round-trip** — Generate random valid environment payloads, create + retrieve, verify equivalence
    - **Property 4: Environment validation rejects invalid input** — Generate random invalid payloads (bad networking type, missing fields), verify 422 rejection
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 4.1**

- [x] 3. Docker sandbox
  - [x] 3.1 Implement sandbox protocol and Docker implementation
    - Create `linchpin-api/app/sandbox.py` with `SandboxProtocol` and `DockerSandbox` class
    - Implement `create` (pull/use base image, attach to correct network), `exec` (run command, return stdout/stderr), `write_file`, `read_file`, `destroy`
    - Use docker-py SDK with Docker socket
    - _Requirements: 14.1, 14.2, 14.3, 14.5_

  - [x] 3.2 Implement Docker network pre-creation at startup
    - In lifespan handler, create `linchpin-none` (internal, no external access) and `linchpin-open` (with external access) Docker networks if they don't exist
    - _Requirements: 14.4, 5.3, 5.4_

- [x] 4. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Session CRUD and event store
  - [x] 5.1 Implement session CRUD endpoints
    - Create `linchpin-api/app/routes/sessions.py`
    - POST `/v1/sessions` — validate agent_id/environment_id exist, provision Docker container via sandbox, create session record, return 201
    - GET `/v1/sessions` — paginated list
    - GET `/v1/sessions/{id}` — fetch by id with stats and usage, return 404 if not found
    - DELETE `/v1/sessions/{id}` — set status to terminated, destroy container
    - POST `/v1/sessions/{id}/archive` — set archived_at, reject if already archived
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6, 6.5, 19.2, 19.3, 20.1, 20.2, 20.3_

  - [x] 5.2 Implement event store with cursor generation and pagination
    - Create `linchpin-api/app/events.py`
    - Implement `append_event` — assign monotonic seq, generate opaque cursor (base64-encoded seq), insert into events table, update session stats/last_event_cursor
    - Implement `get_events` — cursor-based pagination with after_cursor, limit, next_cursor logic
    - _Requirements: 7.3, 8.1, 8.2, 8.3, 8.4_

  - [ ]* 5.3 Write property tests for event structure and pagination
    - **Property 6: Event structure invariants** — Generate random event sequences, verify cursor uniqueness and seq monotonicity
    - **Property 7: Cursor-based event filtering** — Generate random event logs + cursors, verify correct subset returned in ascending order
    - **Property 8: Event pagination** — Generate random event logs + pagination params, verify ordering, limit, next_cursor presence/absence
    - **Validates: Requirements 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 8.4**

  - [x] 5.4 Implement event posting endpoint
    - POST `/v1/sessions/{id}/events` — validate event type against taxonomy, append event, trigger LISTEN/NOTIFY
    - Return 422 for invalid event types, 404 for missing session, 409 for archived session
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 10.4_

  - [ ]* 5.5 Write property test for archived session rejection
    - **Property 12: Archived session rejects new events**
    - Generate random events for archived sessions, verify all rejected and event log unchanged
    - **Validates: Requirements 19.3**

  - [x] 5.6 Implement SSE streaming endpoint
    - GET `/v1/sessions/{id}/stream` — cursor-based replay of existing events, then live streaming via PG LISTEN/NOTIFY
    - JSON-encode each event with type, cursor, payload fields
    - Handle client disconnect cleanup, invalid cursor → 422
    - _Requirements: 7.1, 7.2, 7.4_

- [x] 6. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Policy evaluator and model provider adapters
  - [x] 7.1 Implement policy evaluator
    - Create `linchpin-api/app/policy.py` with `PolicyEvaluator` class
    - Dict-based permission lookup from agent's tool list; return `always_allow`, `always_ask`, or deny if tool not in list
    - O(1) lookup via dict construction from agent's tools
    - _Requirements: 15.1, 15.2, 15.3, 15.4_

  - [ ]* 7.2 Write property test for policy evaluation
    - **Property 10: Policy evaluation correctness**
    - Generate random agents + tool names, verify correct permission returned; unknown tools → denied
    - **Validates: Requirements 15.1, 15.2, 15.3**

  - [x] 7.3 Implement model provider adapters
    - Create `linchpin-api/app/providers.py` with `ModelProviderProtocol`, `AnthropicProvider`, `OpenAIProvider`, `OllamaProvider`
    - Each adapter: construct provider-specific HTTP request, parse response into unified `ModelResponse` (text, tool_use, thinking)
    - Implement retry with exponential backoff (3 attempts) for retryable errors
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_

  - [ ]* 7.4 Write property test for model provider retry
    - **Property 14: Model provider retry with exponential backoff**
    - Generate random failure sequences, verify retry up to 3 times with backoff; after 3 failures → signal failure
    - **Validates: Requirements 12.4, 12.5**

- [x] 8. Orchestrator loop
  - [x] 8.1 Implement orchestrator core loop
    - Create `linchpin-api/app/orchestrator.py`
    - One async task per session: construct context from event log → send to model provider → handle response
    - On text response → emit `agent.message`, transition to `idle`
    - On tool_use → emit `agent.tool_use`, evaluate policy, execute or wait for confirmation
    - On thinking → emit `agent.thinking`, continue
    - Block on PG LISTEN/NOTIFY for session channel when waiting for input
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 6.2, 6.3, 6.4, 6.7_

  - [ ]* 8.2 Write property test for session state machine transitions
    - **Property 5: Session state machine transitions**
    - Generate random state + event combinations, verify transition rules; terminal states reject transitions
    - **Validates: Requirements 6.1, 6.4, 6.7**

  - [x] 8.3 Implement built-in tools
    - Create `linchpin-api/app/tools.py`
    - Implement 8 tools: bash (exec in container), read (read_file), write (write_file), edit (read + apply edits + write), glob (exec `find`/glob in container), grep (exec `grep`/`rg` in container), web_fetch (httpx GET), web_search (stub returning not-implemented message)
    - Each tool: validate parameters, execute via sandbox or httpx, return result dict
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8_

  - [x] 8.4 Wire orchestrator startup into session creation
    - When POST `/v1/sessions` creates a session, start the orchestrator async task
    - Wire event posting to trigger LISTEN/NOTIFY so orchestrator wakes up
    - _Requirements: 5.5, 9.1_

- [x] 9. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Linchpin-connector service
  - [x] 10.1 Implement connector HTTP server with /tools/invoke endpoint
    - Create `linchpin-connector/app/main.py` with FastAPI app
    - POST `/tools/invoke` — accept `ToolInvokeRequest`, route to MCP manager or HTTP tool invoker based on `tool_type`
    - Return tool result or error response with status code and details
    - _Requirements: 16.2, 16.3, 17.1, 17.2_

  - [x] 10.2 Implement MCP manager
    - Create `linchpin-connector/app/mcp.py`
    - Spawn MCP server subprocesses per session using stdio transport
    - Forward tool calls to appropriate subprocess, return results
    - Pass credentials via environment variables
    - Handle subprocess crashes with error response
    - Configurable timeout (default 30s)
    - _Requirements: 16.1, 16.3, 16.4, 16.5, 16.6_

  - [x] 10.3 Implement custom HTTP tool invoker
    - Create `linchpin-connector/app/http_tools.py`
    - Make HTTP request to tool's configured endpoint with tool call parameters
    - Return result or error response with HTTP status code
    - Configurable timeout (default 30s)
    - _Requirements: 17.2, 17.4_

  - [x] 10.4 Wire connector tool calls into orchestrator
    - In orchestrator, when tool_use is for MCP or custom_http tool, forward to connector via `POST /tools/invoke`
    - Emit `agent.mcp_tool_result` or handle `agent.custom_tool_use` / `user.custom_tool_result` flow
    - _Requirements: 16.2, 16.4, 17.1, 17.3_

- [x] 11. Interrupt, confirmation flow, and session TTL
  - [x] 11.1 Implement user interrupt handling
    - In orchestrator, on `user.interrupt` event: cancel current model call, transition to `idle`
    - _Requirements: 9.3, 6.5_

  - [x] 11.2 Implement tool confirmation flow
    - In orchestrator, on `always_ask` policy: emit `session.requires_action`, block on LISTEN/NOTIFY
    - On `user.tool_confirmation` received: proceed with or abort tool call based on confirmation payload
    - _Requirements: 9.2, 11.5, 6.7_

  - [x] 11.3 Implement session TTL and cleanup
    - Background task to check sessions with `ttl_seconds` set; terminate expired sessions (last activity + TTL < now)
    - On terminate/fail: destroy Docker container via sandbox
    - _Requirements: 19.1, 19.2_

- [x] 12. Process restart recovery
  - [x] 12.1 Implement session recovery on startup
    - On API startup, query for sessions with status in (running, idle, rescheduling)
    - Set each to `rescheduling`, emit `session.status_rescheduled` event
    - Replay event log to reconstruct conversation context
    - Transition to `idle`, start orchestrator task
    - Handle missing containers (set to `failed`)
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 6.8_

  - [ ]* 12.2 Write property test for event log replay
    - **Property 13: Event log replay reconstructs context**
    - Generate random event logs, verify replay reconstructs equivalent conversation context
    - **Validates: Requirements 23.2**

- [x] 13. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. Docker Compose and deployment
  - [x] 14.1 Create docker-compose.yml
    - Define services: linchpin-api, linchpin-connector, postgres
    - Mount Docker socket into linchpin-api container
    - Configure internal network between api and connector
    - Set environment variables: DATABASE_URL, LINCHPIN_API_KEY, CONNECTOR_URL
    - Configure postgres with volume for data persistence
    - _Requirements: 22.1, 22.2, 22.3, 22.4_

  - [x] 14.2 Create Dockerfiles for linchpin-api and linchpin-connector
    - Python 3.12 base image, install dependencies, copy source, set entrypoint with uvicorn
    - _Requirements: 22.1_

- [ ] 15. Integration tests
  - [ ]* 15.1 Write integration tests for session lifecycle
    - Create agent → create environment → create session → send message → receive events → terminate
    - Verify full end-to-end flow with real Postgres and Docker
    - _Requirements: 1.1, 3.1, 5.1, 9.1, 6.5_

  - [ ]* 15.2 Write integration tests for SSE streaming
    - Open stream → emit events → verify delivery → reconnect with cursor → verify replay
    - _Requirements: 7.1, 7.2_

  - [ ]* 15.3 Write integration tests for Docker sandbox
    - Create container → exec command → write/read file → destroy
    - Verify linchpin-none isolates, linchpin-open allows external access
    - _Requirements: 14.1, 14.2, 14.3, 14.4_

- [x] 16. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation after each major phase
- Property tests validate the 14 correctness properties from the design using Hypothesis
- Unit tests validate specific examples and edge cases
- All code is Python 3.12, using FastAPI, asyncpg, Pydantic, docker-py, httpx
