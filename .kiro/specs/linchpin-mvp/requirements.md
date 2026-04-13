# Requirements Document

## Introduction

Linchpin is an open standard and self-hostable runtime for managed AI agents. It enables developers to run a managed-agent system on their own infrastructure, with any model provider, and without vendor lock-in. The MVP delivers five core capabilities: agent creation, environment creation, session execution inside Docker containers, SSE event streaming with cursor-based pagination, and tool execution with configurable permissions. The system targets deployment on a single VM via docker-compose and consists of two Python processes (linchpin-api and linchpin-connector) plus Postgres.

## Glossary

- **Linchpin_API**: The primary FastAPI service that exposes the HTTP surface, SSE/stream endpoint, control-plane CRUD, orchestrator loop, sandbox adapter, and built-in tool implementations.
- **Linchpin_Connector**: A secondary Python service that handles MCP server management (stdio transport) and custom HTTP tool invocation. Exposes a single internal endpoint: `POST /tools/invoke`.
- **Agent**: A resource representing a configured AI agent, consisting of a model reference, system prompt, tool list, optional MCP server configurations, and per-tool permissions.
- **Environment**: A resource representing a container template with configurable networking (none or unrestricted).
- **Session**: A resource representing a running interaction between a user and an agent inside a per-session Docker container.
- **Event**: An append-only record associated with a session, identified by an opaque cursor and internal monotonic sequence number.
- **Orchestrator**: An async task within Linchpin_API that drives the agent loop for a single session, blocking on Postgres LISTEN/NOTIFY for input events and streaming model responses.
- **Sandbox**: An abstraction over container runtimes (Docker only in MVP) providing create, exec, write_file, read_file, and destroy operations.
- **Policy_Evaluator**: A component within Linchpin_API that evaluates per-tool permission policies (always_allow or always_ask) to determine whether tool execution requires user confirmation.
- **MCP_Server**: A Model Context Protocol server spawned as a subprocess by Linchpin_Connector using stdio transport.
- **Cursor**: An opaque string identifier for an event, used for pagination and SSE replay.
- **Model_Provider**: An adapter within Linchpin_API that communicates with an external LLM API (Anthropic, OpenAI, or Ollama in MVP).
- **Built_In_Tool**: One of eight tools implemented directly in Linchpin_API: bash, read, write, edit, glob, grep, web_fetch, web_search.
- **Custom_Tool**: A user-defined tool with an HTTP endpoint, invoked via Linchpin_Connector.
- **SSE_Stream**: A Server-Sent Events connection that delivers real-time session events to clients.

## Requirements

### Requirement 1: Agent Creation

**User Story:** As a developer, I want to create an agent with a model, system prompt, tools, and optional MCP servers, so that I can define reusable AI agent configurations.

#### Acceptance Criteria

1. WHEN a valid agent creation request is received, THE Linchpin_API SHALL create an Agent resource with id, name, version, model, system prompt, tools list, mcp_servers list, and created_at timestamp.
2. WHEN an agent creation request specifies a model, THE Linchpin_API SHALL validate that the model object contains a provider field set to one of: anthropic, openai, or ollama, and an id field.
3. WHEN an agent creation request specifies a model with provider set to ollama, THE Linchpin_API SHALL accept an optional base_url field on the model object.
4. WHEN an agent creation request includes tools, THE Linchpin_API SHALL validate that each tool entry contains a permission field set to either always_allow or always_ask.
5. IF an agent creation request contains invalid or missing required fields, THEN THE Linchpin_API SHALL return an HTTP 422 response with a descriptive error message.
6. WHEN an agent is successfully created, THE Linchpin_API SHALL return an HTTP 201 response containing the full Agent resource.

### Requirement 2: Agent Retrieval and Listing

**User Story:** As a developer, I want to retrieve and list agents, so that I can inspect and manage my agent configurations.

#### Acceptance Criteria

1. WHEN a GET request is received for a specific agent by id, THE Linchpin_API SHALL return the full Agent resource.
2. WHEN a GET request is received for the agents collection, THE Linchpin_API SHALL return a paginated list of Agent resources.
3. IF a GET request references an agent id that does not exist, THEN THE Linchpin_API SHALL return an HTTP 404 response with a descriptive error message.

### Requirement 3: Environment Creation

**User Story:** As a developer, I want to create an environment with configurable networking, so that I can define container templates for agent sessions.

#### Acceptance Criteria

1. WHEN a valid environment creation request is received, THE Linchpin_API SHALL create an Environment resource with id, name, config, and created_at timestamp.
2. WHEN an environment creation request specifies a networking config, THE Linchpin_API SHALL validate that the networking type is set to either none or unrestricted.
3. IF an environment creation request contains invalid or missing required fields, THEN THE Linchpin_API SHALL return an HTTP 422 response with a descriptive error message.
4. WHEN an environment is successfully created, THE Linchpin_API SHALL return an HTTP 201 response containing the full Environment resource.

### Requirement 4: Environment Retrieval and Listing

**User Story:** As a developer, I want to retrieve and list environments, so that I can inspect and manage my container templates.

#### Acceptance Criteria

1. WHEN a GET request is received for a specific environment by id, THE Linchpin_API SHALL return the full Environment resource.
2. WHEN a GET request is received for the environments collection, THE Linchpin_API SHALL return a paginated list of Environment resources.
3. IF a GET request references an environment id that does not exist, THEN THE Linchpin_API SHALL return an HTTP 404 response with a descriptive error message.

### Requirement 5: Session Creation and Lifecycle

**User Story:** As a developer, I want to start a session that runs an agent inside a per-session Docker container, so that I can interact with the agent in an isolated execution environment.

#### Acceptance Criteria

1. WHEN a valid session creation request is received with an agent_id and environment_id, THE Linchpin_API SHALL create a Session resource with id, agent_id, agent_version, environment_id, status, container_id, title, metadata, created_at, updated_at, last_event_cursor, ttl_seconds, stats, and usage fields.
2. WHEN a session is created, THE Linchpin_API SHALL provision a new Docker container using the referenced Environment configuration and the pre-built base image (Ubuntu 22.04 with Python 3.12, Node 20, git, curl, jq, ripgrep).
3. WHEN a session is created with an environment that has networking set to none, THE Sandbox SHALL attach the container to the linchpin-none Docker network.
4. WHEN a session is created with an environment that has networking set to unrestricted, THE Sandbox SHALL attach the container to the linchpin-open Docker network.
5. WHEN a session is successfully created, THE Linchpin_API SHALL start an Orchestrator async task for that session.
6. WHEN a session is successfully created, THE Linchpin_API SHALL return an HTTP 201 response containing the full Session resource with status set to running.

### Requirement 6: Session State Machine

**User Story:** As a developer, I want sessions to follow a well-defined state machine, so that I can reliably track and react to session lifecycle changes.

#### Acceptance Criteria

1. THE Session SHALL transition through the following states only: rescheduling, running, idle, terminated, failed.
2. WHEN the Orchestrator begins processing a session, THE Session status SHALL be set to running.
3. WHEN the Orchestrator completes a model turn and no further action is required, THE Session status SHALL be set to idle.
4. WHEN a user sends a message to an idle session, THE Session status SHALL transition from idle to running.
5. WHEN a session is explicitly terminated by the user, THE Session status SHALL be set to terminated.
6. IF the Orchestrator encounters an unrecoverable error, THEN THE Session status SHALL be set to failed and a session.error event SHALL be emitted.
7. WHILE a tool confirmation is pending, THE Session status SHALL remain running.
8. WHEN the Linchpin_API process restarts, THE Orchestrator SHALL recover active sessions by replaying the event log, and THE Session status SHALL be set to rescheduling during recovery.

### Requirement 7: Event Streaming via SSE

**User Story:** As a developer, I want to receive real-time session events over SSE, so that I can build responsive UIs and integrations.

#### Acceptance Criteria

1. WHEN a client opens an SSE connection to a session's stream endpoint, THE Linchpin_API SHALL deliver all new events for that session in real time.
2. WHEN a client opens an SSE connection with a cursor parameter, THE Linchpin_API SHALL replay all events after the specified cursor before switching to live streaming.
3. THE Linchpin_API SHALL assign each event an opaque cursor string and an internal monotonic sequence number.
4. WHEN an event is emitted, THE SSE_Stream SHALL deliver the event as a JSON-encoded SSE message containing the event type, cursor, and payload.

### Requirement 8: Event History with Cursor-Based Pagination

**User Story:** As a developer, I want to retrieve past session events with cursor-based pagination, so that I can replay or audit session history.

#### Acceptance Criteria

1. WHEN a GET request is received for a session's events endpoint, THE Linchpin_API SHALL return a paginated list of Event resources ordered by sequence number.
2. WHEN a GET request includes an after_cursor parameter, THE Linchpin_API SHALL return only events with a sequence number greater than the event identified by the cursor.
3. WHEN a GET request includes a limit parameter, THE Linchpin_API SHALL return at most the specified number of events.
4. THE Linchpin_API SHALL include a next_cursor field in the paginated response when additional events exist beyond the returned page.

### Requirement 9: Sending Events to a Session

**User Story:** As a developer, I want to send messages and confirmations to a session, so that I can interact with the running agent.

#### Acceptance Criteria

1. WHEN a POST request is received with a user.message event for a session, THE Linchpin_API SHALL append the event to the session's event log and notify the Orchestrator via Postgres LISTEN/NOTIFY.
2. WHEN a POST request is received with a user.tool_confirmation event, THE Linchpin_API SHALL append the event and THE Orchestrator SHALL proceed with or abort the pending tool call based on the confirmation payload.
3. WHEN a POST request is received with a user.interrupt event, THE Orchestrator SHALL stop the current model turn and set the session status to idle.
4. WHEN a POST request is received with a user.custom_tool_result event, THE Linchpin_API SHALL append the event and THE Orchestrator SHALL continue processing with the provided tool result.
5. IF a POST request references a session that does not exist, THEN THE Linchpin_API SHALL return an HTTP 404 response.

### Requirement 10: Event Taxonomy

**User Story:** As a developer, I want a well-defined set of event types, so that I can reliably parse and handle session events.

#### Acceptance Criteria

1. THE Linchpin_API SHALL support the following user event types: user.message, user.interrupt, user.tool_confirmation, user.custom_tool_result.
2. THE Linchpin_API SHALL support the following agent event types: agent.message, agent.thinking, agent.tool_use, agent.tool_result, agent.mcp_tool_use, agent.mcp_tool_result, agent.custom_tool_use.
3. THE Linchpin_API SHALL support the following session event types: session.status_running, session.status_idle, session.status_rescheduled, session.status_terminated, session.error, session.requires_action.
4. IF an event is received with an unrecognized type, THEN THE Linchpin_API SHALL return an HTTP 422 response with a descriptive error message.


### Requirement 11: Orchestrator Loop

**User Story:** As a developer, I want the orchestrator to autonomously drive the agent loop, so that the agent can reason, call tools, and respond without manual intervention.

#### Acceptance Criteria

1. WHEN the Orchestrator receives an input event, THE Orchestrator SHALL construct the conversation context from the session's event log and send it to the configured Model_Provider.
2. WHEN the Model_Provider returns a text response, THE Orchestrator SHALL emit an agent.message event and set the session status to idle.
3. WHEN the Model_Provider returns a tool_use response, THE Orchestrator SHALL emit an agent.tool_use event and invoke the Policy_Evaluator to determine whether confirmation is required.
4. WHEN the Policy_Evaluator determines a tool call has always_allow permission, THE Orchestrator SHALL execute the tool immediately and emit an agent.tool_result event.
5. WHEN the Policy_Evaluator determines a tool call has always_ask permission, THE Orchestrator SHALL emit a session.requires_action event and wait for a user.tool_confirmation event.
6. WHEN the Orchestrator is waiting for input, THE Orchestrator SHALL block on Postgres LISTEN/NOTIFY for the session's channel.
7. WHEN the Model_Provider returns a thinking response, THE Orchestrator SHALL emit an agent.thinking event.

### Requirement 12: Model Provider Adapters

**User Story:** As a developer, I want to use different LLM providers, so that I am not locked into a single vendor.

#### Acceptance Criteria

1. THE Linchpin_API SHALL support Anthropic as a Model_Provider by communicating with the Anthropic Messages API.
2. THE Linchpin_API SHALL support OpenAI as a Model_Provider by communicating with the OpenAI Chat Completions API.
3. THE Linchpin_API SHALL support Ollama as a Model_Provider by communicating with the Ollama API at a configurable base_url.
4. WHEN a model provider request fails with a retryable error, THE Model_Provider adapter SHALL retry the request with exponential backoff up to 3 attempts.
5. IF a model provider request fails after all retry attempts, THEN THE Orchestrator SHALL emit a session.error event with the error details.

### Requirement 13: Built-In Tool Execution

**User Story:** As a developer, I want the agent to execute built-in tools inside the sandbox, so that the agent can interact with the container filesystem and run commands.

#### Acceptance Criteria

1. WHEN the Orchestrator receives a tool call for the bash tool, THE Sandbox SHALL execute the command inside the session's Docker container and return stdout and stderr.
2. WHEN the Orchestrator receives a tool call for the read tool, THE Sandbox SHALL read the specified file from the session's Docker container and return its contents.
3. WHEN the Orchestrator receives a tool call for the write tool, THE Sandbox SHALL write the provided content to the specified file path inside the session's Docker container.
4. WHEN the Orchestrator receives a tool call for the edit tool, THE Sandbox SHALL apply the specified edits to the target file inside the session's Docker container.
5. WHEN the Orchestrator receives a tool call for the glob tool, THE Sandbox SHALL execute a glob pattern match inside the session's Docker container and return matching file paths.
6. WHEN the Orchestrator receives a tool call for the grep tool, THE Sandbox SHALL execute a text search inside the session's Docker container and return matching lines with file paths and line numbers.
7. WHEN the Orchestrator receives a tool call for the web_fetch tool, THE Linchpin_API SHALL fetch the specified URL and return the response body.
8. WHEN the Orchestrator receives a tool call for the web_search tool, THE Linchpin_API SHALL return a stub response indicating that web search is not yet implemented.

### Requirement 14: Docker Sandbox Management

**User Story:** As a developer, I want sessions to run in isolated Docker containers, so that agent execution is sandboxed and reproducible.

#### Acceptance Criteria

1. THE Sandbox SHALL implement the following operations: create, exec, write_file, read_file, and destroy.
2. WHEN the Sandbox creates a container, THE Sandbox SHALL use the pre-built base image containing Ubuntu 22.04, Python 3.12, Node 20, git, curl, jq, and ripgrep.
3. WHEN the Sandbox destroys a container, THE Sandbox SHALL remove the Docker container and release associated resources.
4. THE Linchpin_API SHALL pre-create two Docker networks at startup: linchpin-none (isolated, no external access) and linchpin-open (unrestricted external access).
5. IF a Sandbox operation fails, THEN THE Sandbox SHALL return a descriptive error that the Orchestrator can include in a session.error event.

### Requirement 15: Permission Policy Evaluation

**User Story:** As a developer, I want to control which tools require confirmation before execution, so that I can balance automation with safety.

#### Acceptance Criteria

1. WHEN an Agent is created with per-tool permissions, THE Policy_Evaluator SHALL store the permission (always_allow or always_ask) for each tool.
2. WHEN the Orchestrator invokes the Policy_Evaluator for a tool call, THE Policy_Evaluator SHALL return the configured permission for that tool.
3. WHEN a tool is not present in the Agent's tool list, THE Policy_Evaluator SHALL deny execution of that tool.
4. THE Policy_Evaluator SHALL evaluate permissions in constant time relative to the number of tools configured on the agent.

### Requirement 16: MCP Tool Execution

**User Story:** As a developer, I want the agent to use MCP-compatible tools, so that I can extend the agent's capabilities with external tool servers.

#### Acceptance Criteria

1. WHEN an Agent is configured with mcp_servers, THE Linchpin_Connector SHALL spawn each MCP server as a subprocess using stdio transport when a session starts.
2. WHEN the Orchestrator receives a tool call for an MCP tool, THE Orchestrator SHALL forward the invocation to the Linchpin_Connector via POST /tools/invoke.
3. WHEN the Linchpin_Connector receives an MCP tool invocation, THE Linchpin_Connector SHALL forward the call to the appropriate MCP_Server subprocess and return the result.
4. WHEN an MCP tool call completes, THE Orchestrator SHALL emit an agent.mcp_tool_result event with the tool output.
5. THE Linchpin_Connector SHALL pass MCP server credentials via environment variables configured on the Agent resource.
6. IF an MCP_Server subprocess crashes, THEN THE Linchpin_Connector SHALL return an error response to the Orchestrator.

### Requirement 17: Custom HTTP Tool Execution

**User Story:** As a developer, I want to define custom tools backed by HTTP endpoints, so that I can integrate the agent with external services.

#### Acceptance Criteria

1. WHEN the Orchestrator receives a tool call for a custom tool, THE Orchestrator SHALL forward the invocation to the Linchpin_Connector via POST /tools/invoke.
2. WHEN the Linchpin_Connector receives a custom tool invocation, THE Linchpin_Connector SHALL make an HTTP request to the tool's configured endpoint with the tool call parameters.
3. WHEN a custom tool call completes, THE Orchestrator SHALL emit an agent.custom_tool_use event before invocation and process the user.custom_tool_result event upon receiving the result.
4. IF a custom tool HTTP request fails, THEN THE Linchpin_Connector SHALL return an error response with the HTTP status code and error details.

### Requirement 18: Authentication

**User Story:** As a developer, I want the API to be protected by an API key, so that unauthorized access is prevented.

#### Acceptance Criteria

1. THE Linchpin_API SHALL require a bearer token in the Authorization header for all API requests.
2. THE Linchpin_API SHALL validate the bearer token against the LINCHPIN_API_KEY environment variable.
3. IF a request is received without a valid bearer token, THEN THE Linchpin_API SHALL return an HTTP 401 response.
4. THE Linchpin_Connector SHALL only accept requests from the Linchpin_API on the internal network.

### Requirement 19: Session TTL and Cleanup

**User Story:** As a developer, I want sessions to automatically expire and clean up resources, so that idle sessions do not consume resources indefinitely.

#### Acceptance Criteria

1. WHEN a session is created with a ttl_seconds value, THE Linchpin_API SHALL terminate the session and destroy the container after the TTL elapses from the last activity.
2. WHEN a session is terminated or fails, THE Sandbox SHALL destroy the associated Docker container.
3. WHEN a session is archived, THE Linchpin_API SHALL set the archived_at timestamp and THE session SHALL no longer accept new events.

### Requirement 20: Session Retrieval and Listing

**User Story:** As a developer, I want to retrieve and list sessions, so that I can monitor and manage active and past sessions.

#### Acceptance Criteria

1. WHEN a GET request is received for a specific session by id, THE Linchpin_API SHALL return the full Session resource including status, stats, and usage.
2. WHEN a GET request is received for the sessions collection, THE Linchpin_API SHALL return a paginated list of Session resources.
3. IF a GET request references a session id that does not exist, THEN THE Linchpin_API SHALL return an HTTP 404 response with a descriptive error message.

### Requirement 21: Database Schema and Migrations

**User Story:** As a developer, I want the database schema managed via migrations, so that schema changes are versioned and reproducible.

#### Acceptance Criteria

1. THE Linchpin_API SHALL use Alembic for database migration management.
2. THE Linchpin_API SHALL define database tables for agents, environments, sessions, and events in Postgres 16.
3. WHEN the Linchpin_API starts, THE Linchpin_API SHALL verify that all migrations have been applied.

### Requirement 22: Deployment via Docker Compose

**User Story:** As a developer, I want to deploy Linchpin with a single docker-compose command, so that setup is simple and reproducible.

#### Acceptance Criteria

1. THE Linchpin_API SHALL provide a docker-compose.yml that defines services for linchpin-api, linchpin-connector, and postgres.
2. WHEN docker-compose up is executed, THE deployment SHALL start all three services with correct networking and volume mounts.
3. THE docker-compose.yml SHALL mount the Docker socket into the linchpin-api container to enable container management.
4. THE docker-compose.yml SHALL configure the LINCHPIN_API_KEY environment variable for authentication.

### Requirement 23: Process Restart Recovery

**User Story:** As a developer, I want the system to recover active sessions after a process restart, so that sessions are not lost due to transient failures.

#### Acceptance Criteria

1. WHEN the Linchpin_API process starts, THE Orchestrator SHALL query Postgres for all sessions with status running, idle, or rescheduling.
2. WHEN the Orchestrator recovers a session, THE Orchestrator SHALL replay the session's event log to reconstruct the conversation context.
3. WHEN a session is being recovered, THE Session status SHALL be set to rescheduling and a session.status_rescheduled event SHALL be emitted.
4. WHEN recovery is complete, THE Session status SHALL transition to idle.
