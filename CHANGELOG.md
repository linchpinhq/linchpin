# Changelog

All notable changes to Linchpin are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Linchpin follows the versioning + deprecation rules described in the project's Release Plan.

## [Unreleased]

_No unreleased changes yet._

## [0.6.0] - 2026-05-17

Outcomes + Multi-agent threads. Linchpin starts looking less like "an API for running an agent" and more like "an API for running a team." Two PRs.

### Added

#### Outcomes

- **`session.outcome`** declarable at session create. Free-text definition (≤4 KB) + weighted rubric (≤16 criteria, weights sum to 1.0 with 1e-6 float tolerance) + agent grader (`{type: "agent", agent_id: …}`). Persisted as JSONB on `sessions.outcome` (Alembic 0013).
- **`OutcomeAgentGrader`** is the v0.6 grader variant — the grader is itself a Linchpin agent. The discriminator leaves room for a deterministic-rubric grader as a future variant without changing existing callers.
- **`POST /v1/sessions/{id}/outcome_evaluations`** records a grader result. Validates `score in [0, 1]`, persists into the new append-only `outcome_evaluations` table, and emits `session.outcome_evaluation_ended`. Auto-terminates the session + emits `session.status_terminated` when the score crosses `success_threshold` (default 0.8). `auto_terminate: false` opts out of the auto-terminate behavior. Threshold is read from the stored outcome (not the request body) so a malicious grader can't lower the bar.
- **`GET /v1/sessions/{id}/outcome_evaluations`** returns the history newest-first.

#### Multi-agent threads

- **`sessions.parent_session_id`** column (Alembic 0014, `ON DELETE SET NULL`). NULL = root session; non-NULL = thread spawned by another session. Indexed for fast "list threads of a parent" reads.
- **`POST /v1/sessions/{parent_id}/threads`** spawns a worker thread. Validates the parent exists + isn't archived, enforces the depth cap (`LINCHPIN_MAX_THREAD_DEPTH`, default 3), forwards to the existing `create_session` flow, stamps `parent_session_id`, seeds the worker with an optional `input_message`, and emits `session.thread_created` on the parent's event log.
- **`GET /v1/sessions/{parent_id}/threads`** returns every thread of a parent (including terminated), oldest-first.
- **Event mirroring.** Every event posted to a thread is also appended to each ancestor's event log with a `thread_id` payload tag so a coordinator watching its own stream sees one interleaved view. NOTIFY fires on each ancestor channel too — SSE subscribers tailing the coordinator wake up on a thread write.
- **`session.thread_terminated`** emitted on the parent when a thread terminates — symmetric with `session.thread_created`.
- **Depth cap defense-in-depth**: the chain walker bails at 32 hops so a pathological cycle in the data can't loop the API forever.

### Event taxonomy additions

- `session.outcome_evaluation_started`, `session.outcome_evaluation_ended`
- `agent.thread_create` (request shape), `session.thread_created`, `session.thread_idled`, `session.thread_terminated` — the webhook event names declared back in v0.2.0 are now fired by the runtime.

### Schema migrations

- `0013_sessions_outcome` — `sessions.outcome` (JSONB), new `outcome_evaluations` table with `(session_id, created_at DESC)` index.
- `0014_sessions_parent` — `sessions.parent_session_id` (self-referential FK), partial index where non-NULL.

### Operator notes

- `LINCHPIN_MAX_THREAD_DEPTH` defaults to 3 — coordinator → worker → grader is the canonical pattern. Increase only when a workflow legitimately needs deeper trees.
- Event-mirroring writes are best-effort: a failure to mirror to an ancestor logs + continues so the thread's own event log isn't held hostage to an upstream NOTIFY hiccup.

## [0.5.0] - 2026-05-16

Multimodal content + Remote MCP + Git repositories. Three medium-sized features bundled into one minor that round out the resource and content surface. Three PRs.

### Added

#### Multimodal content blocks

- **`user.message.content[]`** accepts text + image + document blocks alongside the legacy plain-string shape. Discriminator: `{type: "text" | "image" | "document"}`. Each non-text block carries a `source` whose own discriminator is `base64` / `url` / `file`.
- **`ImageBlock`** — JPEG, PNG, GIF, WebP. Media-type allow-list rejects unknown types at the API boundary.
- **`DocumentBlock`** — PDF, plain text, markdown. Provider routing decides whether the target model supports it.
- **File-source resolution.** Orchestrator inlines `{source: {type: "file", file_id}}` blocks to base64 by reading bytes through the existing FileStore — providers never see Linchpin file ids.
- **OpenRouter translation.** Image blocks rewrite to OpenAI's `image_url` shape; document blocks pass through verbatim so Claude-routed requests get Anthropic's native shape and other models surface clean upstream errors.

#### Remote URL MCP transport

- **`mcp_servers[]`** widens to a discriminated union: `StdioMCPServerConfig` (the existing v0.4 shape, now `type: "stdio"`, default) + new `UrlMCPServerConfig` (`type: "url"` with `url` + `vault_ids`).
- **Legacy stdio config** (no `type` key) continues to validate — a `mode="before"` normalizer defaults to `stdio` so v0.4-era DB rows keep parsing without a migration.
- **`linchpin-connector` MCPRemoteManager.** Mirrors the existing stdio manager's surface (`list_tools`, `invoke`, `stop_server`, `stop_all`). One `httpx.AsyncClient` per (session, server). JSON-RPC over POST handling both `application/json` and `text/event-stream` responses (MCP streamable HTTP transport).
- **Credential-blind by design.** The connector receives the resolved Bearer token via headers from the API; it never reads vault contents directly.

#### Git repository resource (real clone)

- **`GitRepositoryResource`** (`type: "git_repository"`) — generic HTTPS git remote: `url`, `mount_path`, optional `authorization_token` / `branch` / `shallow` (default `true`). Works with GitHub, GitLab, Bitbucket, Gitea, any HTTPS remote.
- **`GithubRepositoryResource` retained** as a legacy alias for v0.2-v0.4 clients; persisted rows canonicalize to `git_repository`.
- **Per-environment cache.** `$LINCHPIN_GIT_CACHE_ROOT/<env_id>/<sha256(url + "@" + branch)>/`. Two sessions in the same env that mount the same (url, branch) share one on-disk clone.
- **Cache hit refresh:** `git fetch` + `git checkout` + `git reset --hard origin/<branch>`. **Cache miss:** clone into `.tmp` then `os.replace` so a concurrent reader never sees a half-cloned tree.
- **Token safety.** Embedded only during clone (`x-access-token:<tok>@host`), then the remote is rewritten to the clean URL so subsequent fetches and `git log` output don't leak the secret. Clone errors redact the token from stderr.
- **session_resources rows** persist `url`, `branch`, `shallow`, and a `has_token: bool` ride-along — the token itself never reaches the row.
- **Branch validation:** `[A-Za-z0-9_./-]+` so shell metacharacters can't slip past the subprocess.

### Operator notes

- `LINCHPIN_GIT_CACHE_ROOT` defaults to `/var/lib/linchpin/git_cache` — give it durable storage (clones are typically the largest single resource volume).
- Per-(env, url, branch) cache entries are immutable in the cache-key sense; rotating the access token on a future session reuses the cache via `git fetch` + URL rewrite.

### Schema migrations

- None. Multimodal blocks ride on the existing `events.payload` JSONB column. MCP discriminator is a backwards-compat-friendly shape change on the existing `agents.mcp_servers` JSONB column. Git repository resource reuses the existing `session_resources` table.

## [0.4.0] - 2026-05-16

Skills — packaged expertise that progressively discloses to the agent. Pairs with v0.3.0's Memory: Memory is history, Skills is expertise. Three PRs.

### Added

#### Skills resource

- **`/v1/skills`** — full surface: `POST /v1/skills` (multipart upload of tar.gz or zip, sniffed by magic bytes; 10 MB cap; SKILL.md frontmatter parsed + validated at upload), `GET /v1/skills` (paginated, `?include_archived=true`), `GET /v1/skills/{id}`, `DELETE /v1/skills/{id}` (soft).
- **`SKILL.md` frontmatter spec.** `name` ≤64 chars, lowercase + digits + hyphen (no leading/trailing/consecutive hyphens, `anthropic`/`claude`/`linchpin` reserved). `description` ≤1024 chars.
- **Content-addressable storage.** Bundle bytes persist under `LINCHPIN_SKILLS_ROOT` (default `/var/lib/linchpin/skills`) at `<sha[:2]>/<sha[2:4]>/<sha>.tar.gz`. Identical uploads dedupe.
- **Re-upload same name updates** the existing row's bundle pointer — operators ship new revisions without churning the skill id.

#### Agent integration

- **`agent.skills[]`** — list of skill ids attached to an agent. `MAX_SKILLS_PER_AGENT = 8` enforced via Pydantic validator on Create/Update; duplicates rejected. POST/PATCH /v1/agents validate every skill id exists + isn't archived.
- **Persisted on `agent_versions`** — when an agent is patched, the skill list is snapshotted alongside model / tools / mcp_servers so sessions pinned to a prior version see the exact skill set the agent was created with.

#### Session integration

- **Sandbox materialization.** At session create, each attached skill's bundle is unpacked into `$TMPDIR/linchpin-sessions/<sid>/skills/<name>/` (tar.gz or zip, with Python 3.12+ `data` filter for traversal safety), then bind-mounted into the container at `/mnt/skills/<name>/` read-only.
- **Progressive disclosure.** Orchestrator's `build_context` prepends a `<linchpin:skills>` block to the system prompt with each skill's `name` + `description` + a read-pointer to `/mnt/skills/<name>/SKILL.md`. Level-1 metadata always in context; level-2 instructions via the agent's `read` tool; level-3 scripts via `bash`.
- **Missing skills tolerated.** A skill deleted after the agent was patched is logged + skipped — the session still boots without it.
- **`terminate_session` wipes the per-session skills cache root** alongside the memory and deliverables cleanup.

#### CLI

- **`linchpin` CLI** exposed via `[project.scripts]`. First verb: `linchpin skill build <path> [--output OUT] [--include-hidden]`.
- Validates SKILL.md with the same parser the API uses on upload — a bundle that builds cleanly is guaranteed to upload cleanly.
- Hidden files + common noise dirs (`.git`, `__pycache__`, `.DS_Store`, `node_modules`, …) excluded by default.

#### Sample skill

- `examples/skills/gh-pr/` — opens and updates GitHub pull requests using the `gh` CLI. Demonstrates the `SKILL.md` frontmatter shape and bundled-scripts pattern.

### Schema migrations

- `0011_skills` — `skills` table with partial unique index on `(workspace_id, name) WHERE archived_at IS NULL` and a `workspace_id` index.
- `0012_agents_skills` — JSONB `skills` column on `agents` + `agent_versions`, default `'[]'`.

### Operator notes

- `LINCHPIN_SKILLS_ROOT` defaults to `/var/lib/linchpin/skills` — separate from Files / Memory roots so operators can put skill bundles on their own volume.
- Skill bundle 10 MB cap (`SKILL_BUNDLE_MAX_BYTES`) — adjust in `app/models.py` if you ship larger bundles.

## [0.3.0] - 2026-05-16

Memory stores — persistent, workspace-scoped, filesystem-mounted state that survives across sessions. The biggest single missing concept in Linchpin per RFC-0001's parity-gap analysis. Six PRs, one minor.

### Added

#### Memory stores + memories + memory versions

- **`/v1/memory_stores`** — full CRUD (`POST/GET/PATCH`, `POST /{id}/archive`, `DELETE`, `?include_archived=true` on list). Workspace-scoped; `name` unique among live (non-archived) stores via a partial index.
- **`/v1/memory_stores/{id}/memories`** — `POST/GET/PATCH/DELETE`, path-addressed (`/preferences/formatting.md`). 100 KB cap per memory (`MEMORY_MAX_BYTES_PER_MEMORY`); optimistic concurrency via `precondition: {type: "content_sha256", value: <sha>}` returning 412 with `expected_sha256` / `actual_sha256` on mismatch.
- **`/v1/memory_stores/{id}/memory_versions`** — immutable snapshot per write (`action: create | update | delete | redact`). Filter by `memory_id`; binary content stream at `.../{ver}/content` (404 once redacted); explicit `POST .../{ver}/redact` zeros bytes + advances head if redacting the live one.
- **`MemoryWriter`** — single canonical write path used by both the HTTP API (`author='api:<actor>'`) and the in-sandbox watcher (`author='session:<sid>'`). Bytes persisted via a dedicated `LocalFileStore` rooted at `LINCHPIN_MEMORY_ROOT` (default `/var/lib/linchpin/memory_stores/`) — content-addressed at `<sha[:2]>/<sha[2:4]>/<sha>`. Two writes of identical bytes share storage.

#### Session integration

- **`MemoryStoreResource`** entries in `resources[]`. Mount at `/mnt/memory/<store_name>/` with `ro`/`rw` based on the resource's `access` field. Per-session cap of 8 stores (`LINCHPIN_MEMORY_MAX_STORES_PER_SESSION`); duplicate stores in the same request are rejected at validation.
- **Per-session host cache + bind mount.** On session create the store is materialized into `${TMPDIR}/linchpin-sessions/<sid>/memory/<name>/`, then bind-mounted into the container with kernel-enforced ro/rw. `terminate_session` wipes the cache and cancels the watcher.
- **`<linchpin:memory>` system-prompt block.** Auto-prepended to the orchestrator's system prompt listing every mounted store with its access mode and instructions, so agents discover their persistent state without any caller-side prompt management.
- **Per-(session, store) writeback watcher.** Read-write stores get one `watchfiles.awatch` task per store: agent writes inside `/mnt/memory/<name>/` flow through `MemoryWriter`, enforce the 100 KB cap with rollback (restore prior head bytes or unlink), refuse symlinks (exfiltration guard), dedupe via sha256 against the head. Read-only stores rely on the kernel ro bind — no watcher.

#### Version retention + GC

- **30-day default retention** (`LINCHPIN_MEMORY_VERSION_RETENTION_DAYS`, hard-capped at 365). Every version row carries an `expires_at` set at write time.
- **`run_memory_gc()` + `cleanup_expired_memory_versions()`** — hourly batched GC pass (`LIMIT 5000` per tick, configurable via `LINCHPIN_MEMORY_GC_BATCH_SIZE`). Tombstones expired rows (zero sha, empty storage_path, `redacted_at` set) and unlinks the underlying FileStore object only when no other live version dedupes to the same path. `LINCHPIN_MEMORY_GC_INTERVAL_SEC` controls cadence; `LINCHPIN_MEMORY_GC=false` skips the task entirely.

#### Resource fold-in

- **`VaultResource`** in `resources[]` (`{type: "vault", vault_id: ...}`). The legacy top-level `vault_ids` field still parses; usage triggers `Linchpin-Deprecation: vault_ids` + a `session.deprecation_used` event (see Deprecated).

### Events

- `memory.write` — API or sandbox memory write with `memory_id`, `seq`, `author`, `action`, `path`.
- `memory.write_rejected` — over-cap rollback from the sandbox watcher with `reason`, `size_bytes`.
- `session.memory_mount_failed` — reserved for future boot-scan / mount-time failures.
- `session.deprecation_used` — request used a shape slated for removal; payload carries `shape` + `migration` strings.

### Schema migrations

- `0010_memory_stores` — three tables (`memory_stores`, `memories`, `memory_versions`). Soft-delete with partial unique indexes: `memory_stores_name_idx ON (workspace_id, name) WHERE archived_at IS NULL`, `memories_path_idx ON (memory_store_id, path) WHERE deleted_at IS NULL`. Versions table has `seq` (monotonic per memory), `content_sha256`, `storage_path`, `action`, `redacted_at`, `expires_at`.

### Deprecated

See [`DEPRECATIONS.md`](./DEPRECATIONS.md) for full migration paths.

- **New:** `vault_ids` on `CreateSessionRequest` — slated for removal in v0.4.0. Migrate to `resources: [{type: "vault", vault_id: ...}]` entries.
- **Extended one minor:** the four v0.2.0 deprecations (`permission_policy` flat string, flat tools list, `session.requires_action` event, `agent.tool_use` for custom tools) were originally targeted for v0.3.0 removal but v0.3.0 ships inside the 60-day minimum-deprecation window of v0.2.0, so removal moves to **v0.4.0**.

### Operator notes

- `LINCHPIN_MEMORY_ROOT` defaults to `/var/lib/linchpin/memory_stores/` — give it durable storage. Distinct from `LINCHPIN_FILES_ROOT` so operators can put memory bytes on a different volume.
- `LINCHPIN_MEMORY_GC=false` is safe for test/maintenance windows; canonical state is in the DB + FileStore and the cron is purely reclamation.

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
