# Changelog

All notable changes to Linchpin are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Linchpin follows the versioning + deprecation rules described in the project's Release Plan.

## [Unreleased]

_No unreleased changes yet._

## [0.2.0] - 2026-05-15

RFC-0001 Parity Phase A. Closes 13 of 14 scope items (item #4 limited-networking enforcement and breaking-bundle phase 2 ship in v0.2.x — surface lands here). Tag-and-release prepared from `main` once the four feature PRs landed.

### Added

#### Wire format + breaking shapes (phase 1)

- **`Linchpin-API-Version` header.** Dated wire-format negotiation: send `Linchpin-API-Version: 2026-05-13` to opt into v2 shapes; omit for v1 (legacy). Server echoes the resolved version back, rejects unknown dates with `400 unsupported_api_version`, and pins each session to the version it was created with via a new `linchpin_api_version` column (Alembic `0008`). Inputs accept both the v1 and v2 shapes for `permission_policy` (`"always_ask"` vs `{"type": "always_ask"}`) and `tools` (flat list vs `linchpin_toolset_20260512` bundle); both auto-normalize to canonical v1 storage.
- _Phase 2 (output translation, event-shape conversion `session.requires_action` → `session.status_idle.stop_reason`, custom-tool event split, `Linchpin-Deprecation` request-mismatch header) ships in v0.2.x._

#### Resources + Files

- **Session Resources framework.** `resources[]` on `POST /v1/sessions` with a discriminated type (`file`, future: `memory_store`, `git_repository`, `vault`). `GET /v1/sessions/{id}/resources` returns the boot-time set. Live add/remove endpoints return `501` in v0.2.0 — landing in v0.2.x per eng-review decision D1.
- **Files API.** Full Anthropic-shape surface: `POST /v1/files` (multipart upload, 500 MB cap configurable via `LINCHPIN_FILES_MAX_BYTES`), `GET /v1/files` (scope-filtered, paginated), `GET /v1/files/{id}`, `GET /v1/files/{id}/content` (streams; 403 unless `downloadable`), `DELETE /v1/files/{id}` (409 if mounted in an active session). Content-addressable local-fs `FileStore` deduplicates by sha256.
- **Sandbox mount machinery.** Sessions boot with each `resources[]` entry bind-mounted into `/mnt/<mount_path>` (read-only). Mount failures emit `session.resource_mount_failed` events but don't block the session. Reserved mount-path prefixes (`/mnt/session/outputs/`, `/mnt/memory/`, system paths) rejected at validation.
- **Deliverables watcher.** Python `watchfiles`-based in-process asyncio task per session. Anything the agent writes under `/mnt/session/outputs/` auto-registers as a deliverable file (`source=deliverable`, `downloadable=true`, scoped to the session). Boot-scan on session resume ingests files written during outages. Aggregate cap via `LINCHPIN_DELIVERABLES_PER_SESSION_CAP_BYTES` (default 1 GB) with `agent.deliverable_dropped` events on overflow.

#### Environments

- **Environment packages.** `EnvironmentConfig.packages` accepts pre-install lists for `apt`, `pip`, `npm`, `cargo`, `gem`, `go`. Identical package sets across environments share a content-hashed derived image (`linchpinhq/sandbox-env:<sha12>`), so only the first session in each set pays the install cost. Names validated against a conservative regex; per-manager cap 256.
- **Environment networking `limited` mode (API surface).** `NetworkingConfig` widens to a discriminated union including `{type: "limited", allowed_hosts: [...], allow_mcp_servers: bool, allow_package_managers: bool}`. Sessions in limited mode attach to a new `linchpin-limited` Docker bridge network. _v0.2.0 ships denial-by-default on this network; the egress proxy that turns allowlists into permitted traffic lands in v0.2.x._
- **Environment archive + delete.** `POST /v1/environments/{id}/archive` (soft-delete) and `DELETE /v1/environments/{id}` (hard-delete with 409 when a running session still references it). `?include_archived=true` on list.

#### Agents

- **Agent archive + versions.** `POST /v1/agents/{id}/archive`, `description` and `metadata` fields, plus `agent_versions` table that snapshots prior state on every `PATCH`. `GET /v1/agents/{id}/versions` returns history.

#### Sessions

- **Cumulative cache-token usage.** `usage.cache_creation_input_tokens` + `usage.cache_read_input_tokens` alongside the existing input/output counters. Atomic update in the orchestrator.
- **Span events.** `span.model_request_start` / `span.model_request_end` with `span_id`, `model`, `model_usage`, `elapsed_ms`. Enables wallclock-accurate latency tracing.
- **Event type filter.** `GET /v1/sessions/{id}/events?types[]=...&types[]=...` filters by event type. Combinable with `after_cursor` for incremental fetches.

#### Sandbox image

- **Richer base image.** `linchpinhq/sandbox:v0.2.0` (Debian trixie): Python 3.13, Node 20, Go 1.22, Rust 1.77, Java 21, Ruby 3.3, PHP 8.4, GCC 13, plus `psql`, `redis-cli`, `rg`, `tree`, `htop`, `git`. Override with `LINCHPIN_SANDBOX_IMAGE`.

#### Webhooks

- **Webhooks scaffold.** `POST/GET/PATCH/DELETE /v1/webhook_endpoints` + `GET /v1/webhook_endpoints/{id}/deliveries`. HMAC-signed outgoing requests (`Linchpin-Signature: t=<ts>,v1=<hex>`, HMAC-SHA256 over `f"{ts}.{payload}"`). At-least-once delivery via a per-process worker that claims rows under `FOR UPDATE SKIP LOCKED` (multi-process safe), backs off exponentially (10s / 1m / 5m / 30m / 1h), and retires after 5 attempts as `exhausted`. Allowlist: `session.created/status_idle/terminated/failed/requires_action`, `vault.credential.created/updated/deleted`. New tables (Alembic `0009`). `LINCHPIN_WEBHOOKS_WORKER=false` disables the in-process worker.

### Deprecated

See [`DEPRECATIONS.md`](./DEPRECATIONS.md) for full migration paths. All four shapes below ship dual-accepting input in v0.2.x and are removed in v0.3.0:

- `permission_policy` as a flat string → `{"type": "..."}` discriminated object.
- Flat `tools: [{type: "builtin", ...}, ...]` list → `tools: {type: "linchpin_toolset_20260512", default_config: {}, configs: [...]}` bundle.
- `session.requires_action` event → `session.status_idle` with `stop_reason: {"type": "requires_action", "event_ids": [...]}`.
- `agent.tool_use` for custom HTTP tools → `agent.custom_tool_use` + `user.custom_tool_result`.

### Schema migrations

- `0003_files` — content-addressable Files API.
- `0004_session_resources` — discriminated resources framework.
- `0005_environments_archived_at` — environment soft-delete.
- `0006_agent_versions_archive_description` — agent version history.
- `0007_files_session_sha_unique` — dedup boundary on deliverables.
- `0008_sessions_linchpin_api_version` — per-session API-version pin.
- `0009_webhooks` — webhook endpoints + deliveries tables.

## [0.1.0] - 2026-05-13

First formal release — the launch baseline. Tags the post-launch contents of `main` so everything before this point is "pre-release" and everything after follows the documented versioning + deprecation rules.

### Added

- **Managed-agent runtime.** Agents (model + system prompt + tools + permissions), environments (sandbox templates), and sessions (per-session Docker container driving the agent loop).
- **HTTP + SSE API** under `/v1`. Bearer-auth (`LINCHPIN_API_KEY`). Cursor-paginated event log; SSE stream with cursor replay.
- **Model providers.** `openrouter` (~200 cloud models via OpenRouter — Claude, GPT, Gemini, Llama, DeepSeek, Mistral, Qwen, …) and `ollama` (local). Both adapters use raw `httpx`; no upstream SDK dependency.
- **Built-in tools.** `bash`, `read`, `write`, `edit`, `glob`, `grep`, `web_fetch`, `web_search` — all executed inside the session's container.
- **Extensibility.** MCP servers over stdio and HTTP tools, both invoked via `linchpin-connector`.
- **Per-tool permissions.** `always_allow` or `always_ask` (blocks on `user.tool_confirmation`).
- **Sandboxing.** Per-session Docker container (Ubuntu 22.04 · Python 3.12 · Node 20 · git · curl · jq · ripgrep). Networking is per-environment: `none` or `unrestricted`.
- **Credential vaults.** Workspace-scoped encrypted credential store (Fernet, keyed off `VAULT_ENCRYPTION_KEY`). Credentials are referenced by name from agent MCP server configs and injected as env vars when the session starts.
- **Web console** (`linchpin-console`). React + Vite UI for browsing agents/environments/sessions and chatting live.
- **Legal.** Apache-2.0 license, `NOTICE`, and `CONTRIBUTING.md` with DCO sign-off requirement.
- **Docs.** User-first `README.md`, internals split to `ARCHITECTURE.md`, per-feature specs under `.kiro/specs/`.

### Stack

FastAPI · React + Vite · Postgres 16 · Docker · `docker-compose.yml` for local dev.

### Hosted

Live at [linchpin.work](https://linchpin.work) on Vercel.

### Notes

- No `Linchpin-API-Version` request header yet. The wire surface is treated as v1; explicit per-request version negotiation arrives in a later release.
- Backend (`pyproject.toml` × 2) and console (`package.json`) currently bump version strings independently. A release script that keeps them in lockstep is planned for the next release.

[Unreleased]: https://github.com/linchpinhq/linchpin/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/linchpinhq/linchpin/releases/tag/v0.1.0
