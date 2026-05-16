"""Pydantic data models for Linchpin API.

Covers: Agent, Environment, Session, Event domain models,
request/response models for all CRUD endpoints, and the
event type taxonomy as a validated literal set.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field, Tag, field_validator


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
    "agent.deliverable",            # PR5 — D5
    "agent.deliverable_dropped",    # PR5 — D5
    # Session events
    "session.status_running",
    "session.status_idle",
    "session.status_rescheduled",
    "session.status_terminated",
    "session.error",
    "session.requires_action",
    "session.resource_mount_failed",  # PR3 — D5
    # Span events — v0.2.0 item #9
    "span.model_request_start",
    "span.model_request_end",
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
    "agent.deliverable",
    "agent.deliverable_dropped",
    "session.status_running",
    "session.status_idle",
    "session.status_rescheduled",
    "session.status_terminated",
    "session.error",
    "session.requires_action",
    "session.resource_mount_failed",
    "span.model_request_start",
    "span.model_request_end",
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


# v0.2.0 breaking bundle — `permission_policy` widens from a flat
# Literal string to a discriminated `{type: ...}` object on the wire,
# but storage stays string-shaped to avoid a migration. The
# ``_normalize_permission_policy`` validator below accepts either input
# shape and produces the canonical string. v1 shape (string) is the
# canonical form; v2 (object) is the wire-only shape that translates in.
PermissionPolicyLiteral = Literal["always_allow", "always_ask"]


def _normalize_permission_policy(v: str | dict | None) -> str:
    """Coerce permission_policy from either wire shape into the canonical
    string. Accepts:

    - ``"always_allow"`` / ``"always_ask"`` (v1, default)
    - ``{"type": "always_allow"}`` / ``{"type": "always_ask"}`` (v2)

    Anything else raises a validation error.
    """
    if v is None:
        return "always_ask"
    if isinstance(v, str):
        if v not in ("always_allow", "always_ask"):
            raise ValueError(
                f"permission_policy {v!r} must be 'always_allow' or 'always_ask'"
            )
        return v
    if isinstance(v, dict):
        t = v.get("type")
        if t not in ("always_allow", "always_ask"):
            raise ValueError(
                f"permission_policy.type {t!r} must be 'always_allow' "
                "or 'always_ask'"
            )
        return t
    raise ValueError(
        f"permission_policy must be a string or {{type: ...}} object, got {type(v).__name__}"
    )


class BuiltinToolItemConfig(BaseModel):
    """A single tool inside a builtin toolset entry."""

    name: str
    permission_policy: PermissionPolicyLiteral = "always_ask"
    enabled: bool = True

    @field_validator("permission_policy", mode="before")
    @classmethod
    def _coerce_pp(cls, v):  # noqa: D401 — pydantic validator
        return _normalize_permission_policy(v)


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
    permission_policy: PermissionPolicyLiteral = "always_ask"
    endpoint: str | None = None  # HTTP endpoint for custom tool invocation

    @field_validator("permission_policy", mode="before")
    @classmethod
    def _coerce_pp(cls, v):  # noqa: D401
        return _normalize_permission_policy(v)


# Discriminated union on the `type` field for clean validation errors
ToolConfig = Annotated[
    Annotated[BuiltinToolConfig, Tag("builtin")]
    | Annotated[CustomToolConfig, Tag("custom")],
    Discriminator("type"),
]


# v0.2.0 breaking bundle — toolset bundle wrapper. The v2 wire shape
# `tools: {type: "linchpin_toolset_20260512", default_config, configs[]}`
# is normalized down to the canonical v1 flat list at request validation
# time. Storage stays flat-list shaped; v2 input gets unwrapped via the
# ``unwrap_toolset_bundle`` helper called by Create/Update validators.
TOOLSET_BUNDLE_TYPE = "linchpin_toolset_20260512"


def unwrap_toolset_bundle(value: Any) -> list[dict] | Any:
    """Accept either a flat list (v1) or a toolset bundle (v2) and return
    the flat list form. Used by request validators so canonical storage
    stays v1-shaped throughout v0.2.x.
    """
    if isinstance(value, dict) and value.get("type") == TOOLSET_BUNDLE_TYPE:
        from app.api_version import tools_v2_to_v1

        return tools_v2_to_v1(value)
    return value


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
    description: str | None = None                          # v0.2.0 item #5
    metadata: dict[str, Any] = Field(default_factory=dict)  # v0.2.0 item #5
    archived_at: datetime | None = None                     # v0.2.0 item #5


class AgentVersion(BaseModel):
    """Historical snapshot of an Agent's config taken on PATCH (v0.2.0 item #5).

    Returned by ``GET /v1/agents/{id}/versions``. ``version`` matches the
    ``agent_version`` field that gets pinned onto a Session at create-time,
    so sessions can faithfully replay against the exact agent config they
    booted under.
    """

    agent_id: str
    version: int
    name: str
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model: ModelConfig
    system: str
    tools: list[ToolConfig] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    snapshotted_at: datetime


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

# Hostname / IP literal validator for `limited` networking allowlists.
# RFC-1123 (no leading hyphen, no trailing dot) plus a permissive nod to
# IPv4 / IPv6 literals. Wildcards like `*.example.com` are admitted — the
# proxy that lands in v0.2.x will interpret them.
_ALLOWED_HOST_RE = re.compile(
    r"^(?:\*\.)?(?:[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9])"
    r"(?:\.(?:[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]))*$"
)
_ALLOWED_IPV4_RE = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|[01]?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d?\d)){3}$"
)
_MAX_ALLOWED_HOSTS = 256


class NoneNetworking(BaseModel):
    """No external network access. Equivalent to the v0.1 `none` mode."""

    type: Literal["none"] = "none"


class UnrestrictedNetworking(BaseModel):
    """Full external network access. Equivalent to the v0.1 `unrestricted` mode."""

    type: Literal["unrestricted"] = "unrestricted"


class LimitedNetworking(BaseModel):
    """Restricted external network access with an explicit allowlist
    (v0.2.0 item #4).

    The shape matches Anthropic's `limited` mode so SDKs cross-port. In
    v0.2.0 only the surface is shipped — the session container attaches
    to an internal bridge network (same denial level as `none`) and the
    allowlist fields are persisted but not yet enforced. The egress
    proxy that turns the allowlist into actual permitted traffic lands
    in v0.2.x, mirroring the v0.1 → v0.2 PR4 split (eng-review D1).
    """

    type: Literal["limited"] = "limited"
    allowed_hosts: list[str] = Field(default_factory=list)
    allow_mcp_servers: bool = False
    allow_package_managers: bool = False

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_allowed_hosts(cls, v: list[str]) -> list[str]:
        if len(v) > _MAX_ALLOWED_HOSTS:
            raise ValueError(
                f"too many allowed_hosts: max {_MAX_ALLOWED_HOSTS}"
            )
        seen: set[str] = set()
        for host in v:
            if not isinstance(host, str) or not host:
                raise ValueError("allowed_hosts entries must be non-empty strings")
            if host != host.strip():
                raise ValueError(
                    f"allowed_hosts entry {host!r} has leading/trailing whitespace"
                )
            if len(host) > 253:
                raise ValueError(
                    f"allowed_hosts entry {host!r} exceeds 253 chars"
                )
            if "/" in host or " " in host or ":" in host:
                # No URL/scheme/port — just the host, please.
                raise ValueError(
                    f"allowed_hosts entry {host!r} contains a forbidden "
                    "character (slash/space/colon). Provide a hostname or "
                    "IPv4 literal only — no scheme, no path, no port."
                )
            if not (_ALLOWED_HOST_RE.match(host) or _ALLOWED_IPV4_RE.match(host)):
                raise ValueError(
                    f"allowed_hosts entry {host!r} is not a valid hostname "
                    "or IPv4 literal"
                )
            if host in seen:
                raise ValueError(f"duplicate allowed_hosts entry {host!r}")
            seen.add(host)
        return v


# Discriminated union — v0.2.0 item #4 widens NetworkingConfig from a
# plain class to a discriminator over three modes. Existing v0.1 payloads
# (`{"type": "none"}` / `{"type": "unrestricted"}`) parse unchanged.
NetworkingConfig = Annotated[
    Annotated[NoneNetworking, Tag("none")]
    | Annotated[UnrestrictedNetworking, Tag("unrestricted")]
    | Annotated[LimitedNetworking, Tag("limited")],
    Discriminator("type"),
]


# Package names use a conservative allowlist that admits every form we care
# about (`pkg`, `pkg==1.2`, `@scope/pkg`, `pkg.subpkg`, `path/to/pkg@v1.0.0`
# for `go install`) while rejecting shell metacharacters that could escape
# the RUN line in the generated Dockerfile.
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._@/+=:~<>!^*-]{1,256}$")
_PACKAGE_MANAGERS = ("apt", "pip", "npm", "cargo", "gem", "go")
_MAX_PACKAGES_PER_MANAGER = 256


class EnvironmentPackages(BaseModel):
    """Per-package-manager pre-install lists (v0.2.0 item #3).

    The lists are stored on the environment and baked into a derived
    Docker image at session boot. Identical package sets share a cached
    image keyed by content hash, so the first session for a given
    environment pays the install cost once and every later session in
    that environment reuses the layer.

    Empty lists are the default; ``derived_image_tag()`` returns ``None``
    in that case and sessions run against the unmodified base image.
    """

    apt: list[str] = Field(default_factory=list)
    pip: list[str] = Field(default_factory=list)
    npm: list[str] = Field(default_factory=list)
    cargo: list[str] = Field(default_factory=list)
    gem: list[str] = Field(default_factory=list)
    go: list[str] = Field(default_factory=list)

    @field_validator("apt", "pip", "npm", "cargo", "gem", "go")
    @classmethod
    def _validate_package_list(cls, v: list[str]) -> list[str]:
        if len(v) > _MAX_PACKAGES_PER_MANAGER:
            raise ValueError(
                f"too many packages: max {_MAX_PACKAGES_PER_MANAGER} per manager"
            )
        seen: set[str] = set()
        for entry in v:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("package names must be non-empty strings")
            if entry != entry.strip():
                raise ValueError(
                    f"package name {entry!r} has leading/trailing whitespace"
                )
            if not _PACKAGE_NAME_RE.match(entry):
                raise ValueError(
                    f"invalid package name {entry!r}: must match "
                    f"{_PACKAGE_NAME_RE.pattern}"
                )
            if entry in seen:
                raise ValueError(f"duplicate package {entry!r} in list")
            seen.add(entry)
        return v

    def is_empty(self) -> bool:
        return not any(getattr(self, mgr) for mgr in _PACKAGE_MANAGERS)

    def normalized(self) -> dict[str, list[str]]:
        """Order-independent representation for hashing.

        Sorts each manager's list so callers that submit packages in
        different orders share the same derived image.
        """
        return {mgr: sorted(getattr(self, mgr)) for mgr in _PACKAGE_MANAGERS}

    def content_hash(self) -> str:
        """Stable sha256 of the normalized package set."""
        payload = json.dumps(self.normalized(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def derived_image_tag(
    packages: EnvironmentPackages | None, *, repo: str = "linchpinhq/sandbox-env"
) -> str | None:
    """Return the tag of the derived image for *packages*, or None.

    ``None`` means "no packages requested — use the base image directly."
    Tags are 12 hex chars of the content hash, enough collision-resistance
    for tens of millions of distinct environments.
    """
    if packages is None or packages.is_empty():
        return None
    return f"{repo}:{packages.content_hash()[:12]}"


class EnvironmentConfig(BaseModel):
    """Environment configuration wrapper."""

    networking: NetworkingConfig
    packages: EnvironmentPackages = Field(default_factory=EnvironmentPackages)  # v0.2.0 item #3


class Environment(BaseModel):
    """Environment domain model."""

    id: str
    name: str
    config: EnvironmentConfig
    created_at: datetime
    archived_at: datetime | None = None  # v0.2.0 item #6


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
    """Cumulative token usage for a session (v0.2.0 item #10).

    Cache fields default to 0 so providers that don't report prompt-caching
    metrics (Ollama, OpenAI Chat Completions today) yield sensible numbers
    rather than nulls. Counters are monotonic for the session lifetime;
    update_usage() in orchestrator.py increments all four atomically.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0  # v0.2.0 item #10
    cache_read_input_tokens: int = 0      # v0.2.0 item #10


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
    resources: list["SessionResource"] = Field(default_factory=list)


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
    description: str | None = None                          # v0.2.0 item #5
    metadata: dict[str, Any] = Field(default_factory=dict)  # v0.2.0 item #5

    @field_validator("tools", mode="before")
    @classmethod
    def _unwrap_toolset_bundle(cls, v):  # noqa: D401 — pydantic validator
        # v0.2.0 breaking bundle — accept v2 `{type: linchpin_toolset_20260512, ...}`
        # input by unwrapping to the canonical v1 flat list before discriminator
        # validation runs.
        return unwrap_toolset_bundle(v)


class UpdateAgentRequest(BaseModel):
    """PATCH /v1/agents/{id} request body — all fields optional."""

    name: str | None = None
    model: ModelConfig | None = None
    system: str | None = None
    tools: list[ToolConfig] | None = None
    mcp_servers: list[MCPServerConfig] | None = None
    description: str | None = None                          # v0.2.0 item #5
    metadata: dict[str, Any] | None = None                  # v0.2.0 item #5

    @field_validator("tools", mode="before")
    @classmethod
    def _unwrap_toolset_bundle(cls, v):  # noqa: D401
        # Same v2 → v1 unwrap as on Create.
        if v is None:
            return None
        return unwrap_toolset_bundle(v)


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
    resources: list["SessionResourceConfig"] = Field(default_factory=list)

    @field_validator("resources")
    @classmethod
    def _mount_paths_unique(cls, value: list[Any]) -> list[Any]:
        # mount_path uniqueness within a single request — DB also enforces
        # via UNIQUE(session_id, mount_path) but we want a clean 422 rather
        # than a 500 from a duplicate-key error.
        paths: list[str] = []
        for resource in value:
            mount_path = getattr(resource, "mount_path", None)
            if mount_path is None:
                continue  # memory_store has no mount_path
            if mount_path in paths:
                raise ValueError(
                    f"duplicate mount_path '{mount_path}' in resources[]"
                )
            paths.append(mount_path)
        return value


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
    description: str | None = None                          # v0.2.0 item #5
    metadata: dict[str, Any] = Field(default_factory=dict)  # v0.2.0 item #5
    archived_at: datetime | None = None                     # v0.2.0 item #5


class EnvironmentResponse(BaseModel):
    """Full environment resource returned by the API."""

    id: str
    name: str
    config: EnvironmentConfig
    created_at: datetime
    archived_at: datetime | None = None  # v0.2.0 item #6


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
    resources: list["SessionResource"] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Files API (v0.2.0)
# ---------------------------------------------------------------------------

FileSource = Literal["upload", "deliverable"]
FileScopeType = Literal["session"]


class FileResponse(BaseModel):
    """File resource returned by the API. Metadata only; bytes stream via /content."""

    id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    source: FileSource
    downloadable: bool
    scope_type: FileScopeType | None = None
    scope_id: str | None = None
    created_at: datetime
    archived_at: datetime | None = None


class PaginatedFilesResponse(BaseModel):
    """Cursor-paginated file list."""

    data: list[FileResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Session Resources (v0.2.0 — Resources framework, item #1)
# ---------------------------------------------------------------------------

SessionResourceType = Literal["file", "memory_store", "github_repository"]
SessionResourceState = Literal["mounted", "unmounting", "unmounted", "failed"]

# Mount paths reserved by the platform — callers cannot mount resources here.
# /mnt/session/outputs/ is owned by the deliverables watcher (v0.2 PR5).
# /mnt/memory/ is reserved for v0.3 memory stores.
# /proc, /sys, /dev, /etc/linchpin/ are kernel / platform namespaces.
RESERVED_MOUNT_PREFIXES: tuple[str, ...] = (
    "/mnt/session/outputs/",
    "/mnt/memory/",
    "/proc",
    "/sys",
    "/dev",
    "/etc/linchpin/",
)


def _validate_mount_path(value: str) -> str:
    """Shared mount_path rules: absolute, no traversal, not reserved,
    no double slashes; trailing slash stripped for canonical form.

    Normalization protects the DB UNIQUE(session_id, mount_path) — without
    it, `/mnt/data` and `/mnt/data/` would be distinct rows that mount the
    same logical path in the sandbox.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("mount_path must be a non-empty string")
    if not value.startswith("/"):
        raise ValueError("mount_path must be absolute (start with '/')")
    # Reject double slashes — they create ambiguous paths and Postgres treats
    # `/a/b` and `/a//b` as distinct UNIQUE keys.
    if "//" in value:
        raise ValueError("mount_path must not contain consecutive slashes")
    # Reject any path segment equal to ".." — catches /a/../b, /../a, /a/..
    if any(seg == ".." for seg in value.split("/")):
        raise ValueError("mount_path must not contain '..' segments")
    # Strip trailing slash for canonical form (but not from the root "/" — already
    # rejected below as a reserved/empty mount).
    if len(value) > 1 and value.endswith("/"):
        value = value.rstrip("/")
    # Reject the bare root — nothing legitimate mounts at "/".
    if value == "/":
        raise ValueError("mount_path must not be the filesystem root '/'")
    for prefix in RESERVED_MOUNT_PREFIXES:
        # Reserved prefix matches if mount_path is the prefix or sits beneath it.
        # Stripping trailing slash lets "/proc" match "/proc/foo" without matching "/procfs".
        normalized = prefix.rstrip("/")
        if value == normalized or value.startswith(normalized + "/"):
            raise ValueError(f"mount_path '{value}' uses reserved prefix '{prefix}'")
    return value


class FileResource(BaseModel):
    """Mount a previously-uploaded file into the sandbox read-only.

    The referenced file must have source='upload' (deliverables cannot be
    re-mounted into a different session — that would be a covert channel).
    """

    type: Literal["file"]
    file_id: str
    mount_path: str

    @field_validator("mount_path")
    @classmethod
    def _validate_mount_path(cls, value: str) -> str:
        return _validate_mount_path(value)


class MemoryStoreResource(BaseModel):
    """Attach a memory store. Reserved for v0.3 — rejected by v0.2 handlers."""

    type: Literal["memory_store"]
    memory_store_id: str
    access: Literal["read_only", "read_write"] = "read_only"
    instructions: str | None = None


class GithubRepositoryResource(BaseModel):
    """Clone a GitHub repo into the sandbox. Reserved for v0.5 — rejected by v0.2 handlers."""

    type: Literal["github_repository"]
    url: str
    mount_path: str
    authorization_token: str | None = None

    @field_validator("mount_path")
    @classmethod
    def _validate_mount_path(cls, value: str) -> str:
        return _validate_mount_path(value)


SessionResourceConfig = Annotated[
    Annotated[FileResource, Tag("file")]
    | Annotated[MemoryStoreResource, Tag("memory_store")]
    | Annotated[GithubRepositoryResource, Tag("github_repository")],
    Discriminator("type"),
]


class SessionResource(BaseModel):
    """Persisted session_resources row exposed via the API.

    PR2 inserts these with ``state='mounted'`` at session-create time but
    does not actually mount anything — sandbox machinery lands in PR3.
    """

    id: str
    session_id: str
    type: SessionResourceType
    mount_path: str
    config: dict[str, Any] = Field(default_factory=dict)
    state: SessionResourceState = "mounted"
    error: str | None = None
    created_at: datetime
    unmounted_at: datetime | None = None


# ---------------------------------------------------------------------------
# Webhooks (v0.2.0 item #14)
# ---------------------------------------------------------------------------

class WebhookEndpoint(BaseModel):
    """Caller-registered HTTP destination for outbound events.

    ``secret`` is returned in the POST /v1/webhook_endpoints response
    only — subsequent reads omit it. Stripe's "you must save this now"
    model; we don't store any way to retrieve it later. If lost, the
    caller rotates by PATCH'ing with ``rotate_secret: true``.
    """

    id: str
    url: str
    enabled_events: list[str] = Field(default_factory=list)
    description: str | None = None
    enabled: bool = True
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    # secret is set ONLY on the POST response; absent on subsequent reads.
    secret: str | None = None


class CreateWebhookEndpointRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    enabled_events: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, max_length=1024)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("enabled_events")
    @classmethod
    def _validate_enabled_events(cls, v: list[str]) -> list[str]:
        from app.webhooks import WEBHOOK_RELEVANT_EVENTS
        if len(v) > 64:
            raise ValueError("enabled_events: max 64 entries")
        unknown = [e for e in v if e not in WEBHOOK_RELEVANT_EVENTS]
        if unknown:
            raise ValueError(
                f"unknown event types: {unknown}. Supported: "
                f"{sorted(WEBHOOK_RELEVANT_EVENTS)}"
            )
        if len(set(v)) != len(v):
            raise ValueError("enabled_events: duplicates not allowed")
        return v


class UpdateWebhookEndpointRequest(BaseModel):
    url: str | None = None
    enabled_events: list[str] | None = None
    description: str | None = None
    enabled: bool | None = None
    rotate_secret: bool = False

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class WebhookDelivery(BaseModel):
    """One delivery attempt record (success or in-flight retry).

    Listed via ``GET /v1/webhook_endpoints/{id}/deliveries`` so operators
    can audit why a downstream system missed an event.
    """

    id: str
    endpoint_id: str
    event_type: str
    status: Literal["pending", "succeeded", "failed", "exhausted"]
    attempts: int
    last_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    next_attempt_at: datetime
    created_at: datetime
    completed_at: datetime | None
