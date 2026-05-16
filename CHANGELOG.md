# Changelog

All notable changes to Linchpin are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Linchpin follows the versioning + deprecation rules described in the project's Release Plan.

## [Unreleased]

### Added

- **Environment packages (v0.2.0 item #3).** `EnvironmentConfig.packages` accepts pre-install lists for six package managers — `apt`, `pip`, `npm`, `cargo`, `gem`, `go`. At session boot, the API builds a content-hashed derived sandbox image (`linchpinhq/sandbox-env:<sha12>`) on top of the base image. Identical package sets across environments share a single cached image, so only the first session pays the install cost. Empty `packages` falls through to the base image at no cost. Package names are validated against a conservative regex to keep them out of shell-metacharacter territory; per-manager cap is 256 packages.
- **Files API (v0.2.0 PR1 — items #1/#2 prep).** `POST /v1/files` (multipart upload), `GET /v1/files` (scope-filtered, paginated), `GET /v1/files/{id}`, `GET /v1/files/{id}/content` (streams; 403 unless `downloadable`), `DELETE /v1/files/{id}`. Content-addressable local-fs `FileStore` deduplicates identical uploads by sha256. Per-file size cap via `LINCHPIN_FILES_MAX_BYTES` (default 500 MB). Storage root via `LINCHPIN_FILES_ROOT` (default `/var/lib/linchpin/files`).
- New `files` table and indexes (Alembic `0003_files`).

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
