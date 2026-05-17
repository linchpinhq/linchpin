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
    "session.memory_mount_failed",    # v0.3 PR4 — per-store memory mount failure
    "session.deprecation_used",       # v0.3 PR6 — request used a deprecated shape
    # Memory events — v0.3
    "memory.write",                   # v0.3 PR4 — API or sandbox memory write
    "memory.write_rejected",          # v0.3 PR4 — sandbox over-cap rollback
    "memory.gc",                      # v0.3 PR5 — GC pass tombstoned versions
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
    "session.memory_mount_failed",
    "session.deprecation_used",
    "memory.write",
    "memory.write_rejected",
    "memory.gc",
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


class StdioMCPServerConfig(BaseModel):
    """MCP server reached via a local subprocess over stdio.

    The legacy v0.4 shape — ``command`` / ``args`` / ``env`` —
    continues to validate as this variant. The ``type`` discriminator
    is optional on the wire: a config without ``type`` is interpreted
    as stdio for backwards compat with rows persisted before v0.5.0.
    """

    type: Literal["stdio"] = "stdio"
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class UrlMCPServerConfig(BaseModel):
    """MCP server reached over HTTP-streamable transport (v0.5.0).

    Auth flows through the session's ``vault_ids[]`` — each vault id
    listed in ``vault_ids`` resolves to a credential the connector
    injects as a Bearer token on the upstream request. OAuth tokens
    refresh automatically using the existing vault credential
    machinery.
    """

    type: Literal["url"]
    name: str
    url: str
    vault_ids: list[str] = Field(default_factory=list)


def _normalize_mcp_server_input(value: object) -> object:
    """Default ``type`` to ``stdio`` so v0.4 shapes keep validating
    after the discriminator landed. Pass anything that's not a plain
    dict through unchanged so model.copy()/.model_dump() round-trips
    don't break.
    """
    if isinstance(value, dict) and "type" not in value:
        return {**value, "type": "stdio"}
    return value


MCPServerConfig = Annotated[
    Annotated[StdioMCPServerConfig, Tag("stdio")]
    | Annotated[UrlMCPServerConfig, Tag("url")],
    Discriminator("type"),
]


def normalize_mcp_servers(values: list[object]) -> list[object]:
    """Apply ``_normalize_mcp_server_input`` to a list — call this from
    Pydantic ``mode="before"`` validators on every field that accepts
    ``mcp_servers``."""
    return [_normalize_mcp_server_input(v) for v in values]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

MAX_SKILLS_PER_AGENT = 8  # v0.4.0 — mirrors Anthropic's max-8 cap.


class Agent(BaseModel):
    """Agent domain model."""

    id: str
    name: str
    version: int = 1
    model: ModelConfig
    system: str
    tools: list[ToolConfig] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)         # v0.4.0
    created_at: datetime
    description: str | None = None                          # v0.2.0 item #5
    metadata: dict[str, Any] = Field(default_factory=dict)  # v0.2.0 item #5
    archived_at: datetime | None = None                     # v0.2.0 item #5

    @field_validator("mcp_servers", mode="before")
    @classmethod
    def _normalize_mcp_servers(cls, v):  # noqa: D401
        # v0.5.0 — DB rows from v0.4 lack the ``type`` discriminator;
        # default to ``stdio`` so they keep validating.
        return normalize_mcp_servers(v) if v is not None else v


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
    skills: list[str] = Field(default_factory=list)         # v0.4.0
    snapshotted_at: datetime

    @field_validator("mcp_servers", mode="before")
    @classmethod
    def _normalize_mcp_servers(cls, v):  # noqa: D401
        return normalize_mcp_servers(v) if v is not None else v


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
    skills: list[str] = Field(default_factory=list)         # v0.4.0
    description: str | None = None                          # v0.2.0 item #5
    metadata: dict[str, Any] = Field(default_factory=dict)  # v0.2.0 item #5

    @field_validator("tools", mode="before")
    @classmethod
    def _unwrap_toolset_bundle(cls, v):  # noqa: D401 — pydantic validator
        # v0.2.0 breaking bundle — accept v2 `{type: linchpin_toolset_20260512, ...}`
        # input by unwrapping to the canonical v1 flat list before discriminator
        # validation runs.
        return unwrap_toolset_bundle(v)

    @field_validator("mcp_servers", mode="before")
    @classmethod
    def _normalize_mcp_servers(cls, v):  # noqa: D401
        # v0.5.0 — entries without ``type`` are interpreted as stdio so
        # rows persisted before the discriminator landed still parse.
        if v is None:
            return v
        return normalize_mcp_servers(v)

    @field_validator("skills")
    @classmethod
    def _enforce_skills_cap(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_SKILLS_PER_AGENT:
            raise ValueError(
                f"agent.skills exceeds {MAX_SKILLS_PER_AGENT}-per-agent cap "
                f"(got {len(v)})"
            )
        if len(set(v)) != len(v):
            raise ValueError("agent.skills contains duplicate skill ids")
        return v


class UpdateAgentRequest(BaseModel):
    """PATCH /v1/agents/{id} request body — all fields optional."""

    name: str | None = None
    model: ModelConfig | None = None
    system: str | None = None
    tools: list[ToolConfig] | None = None
    mcp_servers: list[MCPServerConfig] | None = None
    skills: list[str] | None = None                         # v0.4.0
    description: str | None = None                          # v0.2.0 item #5
    metadata: dict[str, Any] | None = None                  # v0.2.0 item #5

    @field_validator("tools", mode="before")
    @classmethod
    def _unwrap_toolset_bundle(cls, v):  # noqa: D401
        # Same v2 → v1 unwrap as on Create.
        if v is None:
            return None
        return unwrap_toolset_bundle(v)

    @field_validator("mcp_servers", mode="before")
    @classmethod
    def _normalize_mcp_servers(cls, v):  # noqa: D401
        if v is None:
            return v
        return normalize_mcp_servers(v)

    @field_validator("skills")
    @classmethod
    def _enforce_skills_cap(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if len(v) > MAX_SKILLS_PER_AGENT:
            raise ValueError(
                f"agent.skills exceeds {MAX_SKILLS_PER_AGENT}-per-agent cap "
                f"(got {len(v)})"
            )
        if len(set(v)) != len(v):
            raise ValueError("agent.skills contains duplicate skill ids")
        return v


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


# ---------------------------------------------------------------------------
# Multimodal content blocks (v0.5.0)
# ---------------------------------------------------------------------------


# Supported MIME types per block kind. Kept narrow so a typo doesn't silently
# round-trip into a provider request and 4xx there. Anthropic's vision +
# PDF support is the source-of-truth list; OpenRouter mirrors a subset.
_IMAGE_MEDIA_TYPES: frozenset[str] = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
})
_DOCUMENT_MEDIA_TYPES: frozenset[str] = frozenset({
    "application/pdf", "text/plain", "text/markdown",
})


class TextBlock(BaseModel):
    """Plain-text content block."""

    type: Literal["text"]
    text: str


class _Base64Source(BaseModel):
    """Inline base64-encoded payload. The ``data`` field carries the
    raw base64 string — the API doesn't decode it, just hands it to the
    provider in whichever native shape that provider expects.
    """

    type: Literal["base64"]
    media_type: str
    data: str


class _UrlSource(BaseModel):
    """HTTP(S) URL the provider will fetch on the agent's behalf."""

    type: Literal["url"]
    url: str


class _FileSource(BaseModel):
    """Reference to a Files-API upload. The orchestrator resolves the
    sha256 + storage path at send-time so token rotation / re-upload
    on the same file_id always sees fresh bytes.
    """

    type: Literal["file"]
    file_id: str


ContentBlockSource = Annotated[
    Annotated[_Base64Source, Tag("base64")]
    | Annotated[_UrlSource, Tag("url")]
    | Annotated[_FileSource, Tag("file")],
    Discriminator("type"),
]


class ImageBlock(BaseModel):
    """Image input block. Provider routing rejects the message if the
    target model doesn't advertise vision support."""

    type: Literal["image"]
    source: ContentBlockSource

    @field_validator("source")
    @classmethod
    def _check_media_type(cls, v: object) -> object:
        # Reject unknown image media types at the API boundary so a
        # downstream provider 4xx isn't the first signal.
        media_type = getattr(v, "media_type", None)
        if media_type and media_type not in _IMAGE_MEDIA_TYPES:
            raise ValueError(
                f"image media_type {media_type!r} not supported; "
                f"expected one of {sorted(_IMAGE_MEDIA_TYPES)}"
            )
        return v


class DocumentBlock(BaseModel):
    """Document input block (PDF / plain text / markdown). Provider
    routing rejects the message if the target model doesn't advertise
    document support (typically Claude family only)."""

    type: Literal["document"]
    source: ContentBlockSource

    @field_validator("source")
    @classmethod
    def _check_media_type(cls, v: object) -> object:
        media_type = getattr(v, "media_type", None)
        if media_type and media_type not in _DOCUMENT_MEDIA_TYPES:
            raise ValueError(
                f"document media_type {media_type!r} not supported; "
                f"expected one of {sorted(_DOCUMENT_MEDIA_TYPES)}"
            )
        return v


# Block types valid inside ``user.message.content[]``. The orchestrator's
# legacy single-string ``content: "..."`` shape continues to be accepted
# by ``user.message`` events for backwards compat.
UserMessageBlock = Annotated[
    Annotated[TextBlock, Tag("text")]
    | Annotated[ImageBlock, Tag("image")]
    | Annotated[DocumentBlock, Tag("document")],
    Discriminator("type"),
]


def validate_user_message_payload(payload: dict[str, object]) -> dict[str, object]:
    """Validate the ``content`` field of a ``user.message`` event payload.

    Backwards-compat: a plain string is accepted as before. A list is
    validated block-by-block against the ``UserMessageBlock``
    discriminator so a malformed image source / unknown block type
    fails at the API boundary with a 422 rather than reaching the
    provider as opaque JSON.
    """
    from pydantic import TypeAdapter

    content = payload.get("content")
    if content is None:
        return payload
    if isinstance(content, str):
        return payload
    if not isinstance(content, list):
        raise ValueError(
            "user.message.content must be a string or a list of content blocks"
        )
    adapter = TypeAdapter(UserMessageBlock)
    for idx, block in enumerate(content):
        try:
            adapter.validate_python(block)
        except Exception as exc:
            raise ValueError(
                f"user.message.content[{idx}] is invalid: {exc}"
            ) from exc
    return payload


class EventPayload(BaseModel):
    """A single event inside a PostEventsRequest."""

    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _check_user_message_blocks(
        cls, v: dict[str, Any], info
    ) -> dict[str, Any]:
        # v0.5.0 — when the event is a user.message with structured
        # content blocks, run the block-discriminator validator so a
        # malformed block 422s at the API boundary instead of erroring
        # downstream in the provider.
        if (info.data or {}).get("type") == "user.message":
            validate_user_message_payload(v)
        return v


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
    skills: list[str] = Field(default_factory=list)         # v0.4.0
    created_at: datetime
    description: str | None = None                          # v0.2.0 item #5
    metadata: dict[str, Any] = Field(default_factory=dict)  # v0.2.0 item #5
    archived_at: datetime | None = None                     # v0.2.0 item #5

    @field_validator("mcp_servers", mode="before")
    @classmethod
    def _normalize_mcp_servers(cls, v):  # noqa: D401
        return normalize_mcp_servers(v) if v is not None else v


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

SessionResourceType = Literal[
    "file", "memory_store", "github_repository", "git_repository", "vault",
]
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


class GitRepositoryResource(BaseModel):
    """Clone any HTTPS git remote into the sandbox (v0.5.0).

    Generic by design — works with GitHub, GitLab, Bitbucket, Gitea,
    any HTTPS git server — even though the most common pairing is the
    GitHub MCP server. Tokens flow either inline via
    ``authorization_token`` or, post-v0.5, through ``vault_ids`` (same
    pattern as URL MCP servers).
    """

    type: Literal["git_repository"]
    url: str
    mount_path: str
    authorization_token: str | None = None
    branch: str | None = None
    shallow: bool = True

    @field_validator("mount_path")
    @classmethod
    def _validate_mount_path(cls, value: str) -> str:
        return _validate_mount_path(value)


class GithubRepositoryResource(BaseModel):
    """Legacy alias for ``GitRepositoryResource`` (v0.2-v0.4 reserved
    the ``github_repository`` type; v0.5 generalizes to
    ``git_repository``). Accepted on input for backwards compat and
    normalized to the generic form before dispatch.
    """

    type: Literal["github_repository"]
    url: str
    mount_path: str
    authorization_token: str | None = None
    branch: str | None = None
    shallow: bool = True

    @field_validator("mount_path")
    @classmethod
    def _validate_mount_path(cls, value: str) -> str:
        return _validate_mount_path(value)


class VaultResource(BaseModel):
    """Attach a credential vault to the session.

    Folded-in from the legacy ``vault_ids`` field — sending vault_ids on
    the request continues to parse for one more minor but emits a
    ``Linchpin-Deprecation: vault_ids`` header + a
    ``session.deprecation_used`` event. Internal code normalizes both
    inputs into ``resources[]`` of ``type='vault'`` so the rest of the
    pipeline sees a single homogeneous list.

    No ``mount_path`` — vaults inject credentials into the agent
    runtime through ``credentials.py``, not via a sandbox bind mount.
    """

    type: Literal["vault"]
    vault_id: str


SessionResourceConfig = Annotated[
    Annotated[FileResource, Tag("file")]
    | Annotated[MemoryStoreResource, Tag("memory_store")]
    | Annotated[GitRepositoryResource, Tag("git_repository")]
    | Annotated[GithubRepositoryResource, Tag("github_repository")]
    | Annotated[VaultResource, Tag("vault")],
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


# ---------------------------------------------------------------------------
# Memory (v0.3.0)
# ---------------------------------------------------------------------------

# Path validation lives next to its constants so the regex can be reused
# by both API routes and the sandbox watcher. RFC-3986-ish: starts with
# '/', no '..', no '//', no trailing slash, total ≤4096 chars.
_MEMORY_PATH_MAX = 4096
_MEMORY_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_MEMORY_NAME_RESERVED = frozenset({"anthropic", "claude", "linchpin"})
MEMORY_MAX_BYTES_PER_MEMORY = 102_400  # 100 KB — hardcoded; cannot raise at runtime


def validate_memory_path(path: str) -> str:
    """Return *path* if valid; raise ``ValueError`` otherwise.

    Memory paths address an entry inside a store. The rules mirror what
    the sandbox can safely materialize as a file path on disk:

    - must start with ``/``
    - must NOT end with ``/`` (paths are file-shaped, not directory-shaped)
    - no ``..`` or empty segments (no ``//``)
    - no NUL bytes
    - ≤ 4096 chars total
    """
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    if not path:
        raise ValueError("path must not be empty")
    if not path.startswith("/"):
        raise ValueError(f"path {path!r} must start with '/'")
    if path == "/" or path.endswith("/"):
        raise ValueError(f"path {path!r} must not end with '/'")
    if "\x00" in path:
        raise ValueError("path must not contain NUL bytes")
    if len(path) > _MEMORY_PATH_MAX:
        raise ValueError(f"path length {len(path)} exceeds {_MEMORY_PATH_MAX}")
    parts = path.split("/")
    for p in parts[1:]:  # parts[0] is empty because of leading '/'
        if p == "" or p == "..":
            raise ValueError(f"path {path!r} contains '..' or empty segment")
    return path


class MemoryStore(BaseModel):
    """Workspace-scoped persistent memory container.

    A session attaches up to ``LINCHPIN_MEMORY_MAX_STORES_PER_SESSION``
    memory stores via the ``resources[]`` framework. Mount path is
    derived from ``name`` (``/mnt/memory/<name>/``), not caller-supplied.
    """

    id: str
    name: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class CreateMemoryStoreRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1024)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _MEMORY_NAME_RE.match(v):
            raise ValueError(
                f"name {v!r} must be lowercase kebab-case "
                "(letters, digits, hyphens; must start with letter or digit)"
            )
        if v in _MEMORY_NAME_RESERVED:
            raise ValueError(f"name {v!r} is reserved")
        return v


class UpdateMemoryStoreRequest(BaseModel):
    description: str | None = Field(default=None, max_length=1024)


class ContentShaPrecondition(BaseModel):
    """Optimistic-concurrency precondition for memory writes.

    Semantics mirror HTTP ETag: ``sha256=None`` matches "must not exist"
    (create-only); ``sha256=<hex>`` matches "current head sha must
    equal". A mismatch returns 412 ``precondition_failed`` so callers
    can re-read and retry their merge.
    """

    type: Literal["content_sha256"] = "content_sha256"
    sha256: str | None = None


class WriteMemoryRequest(BaseModel):
    """POST / PATCH ``/v1/memory_stores/{id}/memories`` body.

    ``content`` is base64-encoded bytes on the wire (Pydantic ``bytes``
    field type decodes it). 100 KB hard cap is enforced at validation
    time, before the bytes ever touch storage.
    """

    path: str
    content: bytes
    precondition: ContentShaPrecondition | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        return validate_memory_path(v)

    @field_validator("content")
    @classmethod
    def _enforce_cap(cls, v: bytes) -> bytes:
        if len(v) > MEMORY_MAX_BYTES_PER_MEMORY:
            raise ValueError(
                f"content size {len(v)} exceeds cap {MEMORY_MAX_BYTES_PER_MEMORY}"
            )
        return v


class Memory(BaseModel):
    """A single path-addressed memory entry inside a store."""

    id: str
    memory_store_id: str
    path: str
    content_sha256: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime


MemoryVersionAction = Literal["create", "update", "delete", "redact"]


class MemoryVersion(BaseModel):
    """Immutable snapshot of a memory at a point in time.

    Versions retain for ``LINCHPIN_MEMORY_VERSION_RETENTION_DAYS`` (30 by
    default), then the GC pass tombstones them — the row remains for
    audit but ``content_sha256`` becomes zeros and the bytes are
    unlinked from the FileStore.
    """

    id: str
    memory_id: str
    memory_store_id: str
    seq: int
    content_sha256: str
    size_bytes: int
    author: str
    action: MemoryVersionAction
    redacted_at: datetime | None = None
    created_at: datetime
    expires_at: datetime


class RedactRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1024)


# ---------------------------------------------------------------------------
# Skills (v0.4.0)
# ---------------------------------------------------------------------------

# Slug rules mirror memory_stores: lowercase + digits + hyphen, no
# leading/trailing hyphen, no double hyphens, reserved namespaces stripped.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_SKILL_NAME_RESERVED = frozenset({"anthropic", "claude", "linchpin"})

SKILL_NAME_MAX_LEN = 64
SKILL_DESCRIPTION_MAX_LEN = 1024
SKILL_BUNDLE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB cap per skill bundle


def validate_skill_name(value: str) -> str:
    """Reject malformed skill names. Returns the canonical form."""
    if not value:
        raise ValueError("skill name must not be empty")
    if len(value) > SKILL_NAME_MAX_LEN:
        raise ValueError(
            f"skill name must be ≤ {SKILL_NAME_MAX_LEN} characters (got {len(value)})"
        )
    if not _SKILL_NAME_RE.match(value):
        raise ValueError(
            "skill name must match [a-z0-9](?:[a-z0-9-]*[a-z0-9])? — "
            "lowercase letters, digits, and hyphens only; "
            "no leading/trailing hyphen"
        )
    if "--" in value:
        raise ValueError("skill name must not contain consecutive hyphens")
    for reserved in _SKILL_NAME_RESERVED:
        if reserved in value:
            raise ValueError(
                f"skill name must not contain reserved word '{reserved}'"
            )
    return value


class Skill(BaseModel):
    """A skill bundle exposed via the API.

    The bundle bytes themselves (SKILL.md + scripts/resources) live on
    disk under ``LINCHPIN_SKILLS_ROOT`` — the row records only the
    SKILL.md frontmatter (``name``, ``description``) plus the bundle's
    content-addressable storage info.
    """

    id: str
    name: str
    description: str
    bundle_sha256: str
    bundle_size: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class SkillListResponse(BaseModel):
    """Paginated list payload for ``GET /v1/skills``."""

    data: list[Skill]
    has_more: bool
    next_cursor: str | None = None
