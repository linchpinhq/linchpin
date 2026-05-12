"""Pydantic data models for Linchpin API.

Covers: Agent, Environment, Session, Event domain models,
request/response models for all CRUD endpoints, and the
event type taxonomy as a validated literal set.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field, Tag


# ---------------------------------------------------------------------------
# Event type taxonomy
# ---------------------------------------------------------------------------

EVENT_TYPES: frozenset[str] = frozenset({
    # User events
    "user.message",
    "user.interrupt",
    "user.tool_confirmation",
    "user.custom_tool_result",
    # Agent events
    "agent.message",
    "agent.message_delta",
    "agent.thinking",
    "agent.tool_use",
    "agent.tool_result",
    "agent.mcp_tool_use",
    "agent.mcp_tool_result",
    "agent.custom_tool_use",
    # Session events
    "session.status_running",
    "session.status_idle",
    "session.status_rescheduled",
    "session.status_terminated",
    "session.error",
    "session.requires_action",
})

EventType = Literal[
    "user.message",
    "user.interrupt",
    "user.tool_confirmation",
    "user.custom_tool_result",
    "agent.message",
    "agent.message_delta",
    "agent.thinking",
    "agent.tool_use",
    "agent.tool_result",
    "agent.mcp_tool_use",
    "agent.mcp_tool_result",
    "agent.custom_tool_use",
    "session.status_running",
    "session.status_idle",
    "session.status_rescheduled",
    "session.status_terminated",
    "session.error",
    "session.requires_action",
]


def is_valid_event_type(event_type: str) -> bool:
    """Return True if *event_type* belongs to the valid taxonomy."""
    return event_type in EVENT_TYPES


# ---------------------------------------------------------------------------
# Model / Tool / MCP configuration
# ---------------------------------------------------------------------------

class ModelConfig(BaseModel):
    """LLM provider configuration."""

    provider: Literal["openrouter", "ollama"]
    id: str
    base_url: str | None = None  # Override default endpoint for openrouter or ollama


class BuiltinToolItemConfig(BaseModel):
    """A single tool inside a builtin toolset entry."""

    name: str
    permission_policy: Literal["always_allow", "always_ask"] = "always_ask"
    enabled: bool = True


class BuiltinToolConfig(BaseModel):
    """Built-in toolset entry (e.g. bash, read, write …)."""

    type: Literal["builtin"]
    default_config: dict[str, Any] = Field(default_factory=dict)
    configs: list[BuiltinToolItemConfig] = Field(default_factory=list)


class CustomToolConfig(BaseModel):
    """Custom tool entry with an HTTP endpoint."""

    type: Literal["custom"]
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    permission_policy: Literal["always_allow", "always_ask"] = "always_ask"
    endpoint: str | None = None  # HTTP endpoint for custom tool invocation


# Discriminated union on the `type` field for clean validation errors
ToolConfig = Annotated[
    Annotated[BuiltinToolConfig, Tag("builtin")]
    | Annotated[CustomToolConfig, Tag("custom")],
    Discriminator("type"),
]


class MCPServerConfig(BaseModel):
    """MCP server subprocess configuration."""

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent(BaseModel):
    """Agent domain model."""

    id: str
    name: str
    version: int = 1
    model: ModelConfig
    system: str
    tools: list[ToolConfig] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    created_at: datetime


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

class NetworkingConfig(BaseModel):
    """Container networking configuration."""

    type: Literal["none", "unrestricted"]


class EnvironmentConfig(BaseModel):
    """Environment configuration wrapper."""

    networking: NetworkingConfig


class Environment(BaseModel):
    """Environment domain model."""

    id: str
    name: str
    config: EnvironmentConfig
    created_at: datetime


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

SessionStatus = Literal["rescheduling", "running", "idle", "terminated", "failed"]


class SessionStats(BaseModel):
    """Aggregate counters for a session."""

    total_events: int = 0
    tool_calls: int = 0
    model_turns: int = 0


class SessionUsage(BaseModel):
    """Token usage for a session."""

    input_tokens: int = 0
    output_tokens: int = 0


class Session(BaseModel):
    """Session domain model."""

    id: str
    agent_id: str
    agent_version: int
    environment_id: str
    status: SessionStatus = "running"
    container_id: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    last_event_cursor: str | None = None
    ttl_seconds: int | None = None
    stats: SessionStats = Field(default_factory=SessionStats)
    usage: SessionUsage = Field(default_factory=SessionUsage)
    vault_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

class Event(BaseModel):
    """Append-only event record."""

    session_id: str
    cursor: str
    seq: int
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    processed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateAgentRequest(BaseModel):
    """POST /v1/agents request body."""

    name: str
    model: ModelConfig
    system: str
    tools: list[ToolConfig] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)


class UpdateAgentRequest(BaseModel):
    """PATCH /v1/agents/{id} request body — all fields optional."""

    name: str | None = None
    model: ModelConfig | None = None
    system: str | None = None
    tools: list[ToolConfig] | None = None
    mcp_servers: list[MCPServerConfig] | None = None


class CreateEnvironmentRequest(BaseModel):
    """POST /v1/environments request body."""

    name: str
    config: EnvironmentConfig


class CreateSessionRequest(BaseModel):
    """POST /v1/sessions request body."""

    agent_id: str
    environment_id: str
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int | None = None
    vault_ids: list[str] = Field(default_factory=list)


class EventPayload(BaseModel):
    """A single event inside a PostEventsRequest."""

    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


class PostEventsRequest(BaseModel):
    """POST /v1/sessions/{id}/events — batch envelope."""

    events: list[EventPayload]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class AgentResponse(BaseModel):
    """Full agent resource returned by the API."""

    id: str
    name: str
    version: int
    model: ModelConfig
    system: str
    tools: list[ToolConfig] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    created_at: datetime


class EnvironmentResponse(BaseModel):
    """Full environment resource returned by the API."""

    id: str
    name: str
    config: EnvironmentConfig
    created_at: datetime


class SessionResponse(BaseModel):
    """Full session resource returned by the API."""

    id: str
    agent_id: str
    agent_version: int
    environment_id: str
    status: SessionStatus
    container_id: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    last_event_cursor: str | None = None
    ttl_seconds: int | None = None
    stats: SessionStats = Field(default_factory=SessionStats)
    usage: SessionUsage = Field(default_factory=SessionUsage)
    vault_ids: list[str] = Field(default_factory=list)


class EventResponse(BaseModel):
    """Single event in API responses."""

    session_id: str
    cursor: str
    seq: int
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    processed_at: datetime | None = None


class PaginatedEventsResponse(BaseModel):
    """Cursor-paginated event list."""

    events: list[EventResponse]
    next_cursor: str | None = None


class PaginatedListResponse(BaseModel):
    """Generic paginated list for agents, environments, sessions."""

    data: list[Any]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Credential type and provider literals
# ---------------------------------------------------------------------------

CredentialType = Literal["bearer_token", "api_key", "oauth"]
ProviderName = Literal["openrouter", "ollama"]


# ---------------------------------------------------------------------------
# Vault domain model
# ---------------------------------------------------------------------------

class Vault(BaseModel):
    """Vault domain model."""

    id: str
    display_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


# ---------------------------------------------------------------------------
# Credential domain model
# ---------------------------------------------------------------------------

class Credential(BaseModel):
    """Credential domain model (no secret fields)."""

    id: str
    vault_id: str
    credential_type: CredentialType
    mcp_server_url: str | None = None
    provider: ProviderName | None = None
    client_id: str | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


# ---------------------------------------------------------------------------
# Vault request models
# ---------------------------------------------------------------------------

class CreateVaultRequest(BaseModel):
    """POST /v1/vaults request body."""

    display_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Credential request models (discriminated union on credential_type)
# ---------------------------------------------------------------------------

class CreateBearerTokenCredentialRequest(BaseModel):
    """Create a bearer_token credential."""

    credential_type: Literal["bearer_token"]
    mcp_server_url: str
    token: str


class CreateApiKeyCredentialRequest(BaseModel):
    """Create an api_key credential."""

    credential_type: Literal["api_key"]
    provider: ProviderName
    api_key: str


class CreateOAuthCredentialRequest(BaseModel):
    """Create an oauth credential."""

    credential_type: Literal["oauth"]
    mcp_server_url: str
    access_token: str
    refresh_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None


CreateCredentialRequest = Annotated[
    CreateBearerTokenCredentialRequest
    | CreateApiKeyCredentialRequest
    | CreateOAuthCredentialRequest,
    Discriminator("credential_type"),
]


class UpdateCredentialRequest(BaseModel):
    """PATCH body — only secret fields, all optional."""

    token: str | None = None
    api_key: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    client_secret: str | None = None


# ---------------------------------------------------------------------------
# Vault / Credential response models
# ---------------------------------------------------------------------------

class VaultResponse(BaseModel):
    """Full vault resource returned by the API."""

    id: str
    display_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class CredentialResponse(BaseModel):
    """Credential resource returned by the API (all secret fields omitted)."""

    id: str
    vault_id: str
    credential_type: CredentialType
    mcp_server_url: str | None = None
    provider: ProviderName | None = None
    client_id: str | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
