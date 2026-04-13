// ---------------------------------------------------------------------------
// Model / Tool / MCP configuration
// ---------------------------------------------------------------------------

/** LLM provider configuration. */
export interface ModelConfig {
  provider: "anthropic" | "openai" | "ollama";
  id: string;
  base_url?: string;
}

/** A single tool inside a builtin toolset entry. */
export interface BuiltinToolItemConfig {
  name: string;
  permission_policy: "always_allow" | "always_ask";
  enabled: boolean;
}

/** Built-in toolset entry (e.g. bash, read, write). */
export interface BuiltinToolConfig {
  type: "builtin";
  default_config: Record<string, unknown>;
  configs: BuiltinToolItemConfig[];
}

/** Custom tool entry with an HTTP endpoint. */
export interface CustomToolConfig {
  type: "custom";
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  permission_policy: "always_allow" | "always_ask";
  endpoint?: string;
}

/** Discriminated union on the `type` field. */
export type ToolConfig = BuiltinToolConfig | CustomToolConfig;

/** MCP server subprocess configuration. */
export interface MCPServerConfig {
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Agent
// ---------------------------------------------------------------------------

/** Agent domain model. */
export interface Agent {
  id: string;
  name: string;
  version: number;
  model: ModelConfig;
  system: string;
  tools: ToolConfig[];
  mcp_servers: MCPServerConfig[];
  created_at: string;
}

// ---------------------------------------------------------------------------
// Environment
// ---------------------------------------------------------------------------

/** Container networking configuration. */
export interface NetworkingConfig {
  type: "none" | "unrestricted";
}

/** Environment configuration wrapper. */
export interface EnvironmentConfig {
  networking: NetworkingConfig;
}

/** Environment domain model. */
export interface Environment {
  id: string;
  name: string;
  config: EnvironmentConfig;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

export type SessionStatus =
  | "running"
  | "idle"
  | "rescheduling"
  | "terminated"
  | "failed";

/** Aggregate counters for a session. */
export interface SessionStats {
  total_events: number;
  tool_calls: number;
  model_turns: number;
}

/** Token usage for a session. */
export interface SessionUsage {
  input_tokens: number;
  output_tokens: number;
}

/** Session domain model. */
export interface Session {
  id: string;
  agent_id: string;
  agent_version: number;
  environment_id: string;
  status: SessionStatus;
  container_id?: string;
  title?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  archived_at?: string;
  last_event_cursor?: string;
  ttl_seconds?: number;
  vault_ids: string[];
  stats: SessionStats;
  usage: SessionUsage;
}

// ---------------------------------------------------------------------------
// Event
// ---------------------------------------------------------------------------

export type EventType =
  | "user.message"
  | "user.interrupt"
  | "user.tool_confirmation"
  | "user.custom_tool_result"
  | "agent.message"
  | "agent.message_delta"
  | "agent.thinking"
  | "agent.tool_use"
  | "agent.tool_result"
  | "agent.mcp_tool_use"
  | "agent.mcp_tool_result"
  | "agent.custom_tool_use"
  | "session.status_running"
  | "session.status_idle"
  | "session.status_rescheduled"
  | "session.status_terminated"
  | "session.error"
  | "session.requires_action";

/** Append-only event record. */
export interface SessionEvent {
  session_id: string;
  cursor: string;
  seq: number;
  type: EventType;
  payload: Record<string, unknown>;
  processed_at?: string;
}

// ---------------------------------------------------------------------------
// Paginated responses
// ---------------------------------------------------------------------------

/** Generic paginated list for agents, environments, sessions. */
export interface PaginatedResponse<T> {
  data: T[];
  next_cursor?: string;
  has_more: boolean;
}

/** Cursor-paginated event list. */
export interface PaginatedEventsResponse {
  events: SessionEvent[];
  next_cursor?: string;
}

// ---------------------------------------------------------------------------
// Request models
// ---------------------------------------------------------------------------

/** POST /v1/agents request body. */
export interface CreateAgentRequest {
  name: string;
  model: ModelConfig;
  system: string;
  tools: ToolConfig[];
  mcp_servers: MCPServerConfig[];
}

/** PATCH /v1/agents/{id} request body. */
export interface UpdateAgentRequest {
  name?: string;
  model?: ModelConfig;
  system?: string;
}

/** POST /v1/environments request body. */
export interface CreateEnvironmentRequest {
  name: string;
  config: EnvironmentConfig;
}

/** POST /v1/sessions request body. */
export interface CreateSessionRequest {
  agent_id: string;
  environment_id: string;
  title?: string;
  ttl_seconds?: number;
  vault_ids?: string[];
}

/** POST /v1/sessions/{id}/events request body. */
export interface PostEventsRequest {
  events: Array<{
    type: EventType;
    payload: Record<string, unknown>;
  }>;
}

// ---------------------------------------------------------------------------
// Vault & Credential
// ---------------------------------------------------------------------------

export type CredentialType = "bearer_token" | "api_key" | "oauth";
export type ProviderName = "anthropic" | "openai" | "ollama";

/** Vault domain model. */
export interface Vault {
  id: string;
  display_name: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
}

/** Credential response (secrets omitted). */
export interface CredentialResponse {
  id: string;
  vault_id: string;
  credential_type: CredentialType;
  mcp_server_url?: string | null;
  provider?: ProviderName | null;
  client_id?: string | null;
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
}

/** POST /v1/vaults request body. */
export interface CreateVaultRequest {
  display_name: string;
  metadata?: Record<string, unknown>;
}

/** POST /v1/vaults/{vault_id}/credentials request body (union). */
export type CreateCredentialRequest =
  | { credential_type: "bearer_token"; mcp_server_url: string; token: string }
  | { credential_type: "api_key"; provider: ProviderName; api_key: string }
  | {
      credential_type: "oauth";
      mcp_server_url: string;
      access_token: string;
      refresh_token?: string;
      client_id?: string;
      client_secret?: string;
    };

/** PATCH /v1/vaults/{vault_id}/credentials/{credential_id} request body. */
export interface UpdateCredentialRequest {
  token?: string;
  api_key?: string;
  access_token?: string;
  refresh_token?: string;
  client_secret?: string;
}
