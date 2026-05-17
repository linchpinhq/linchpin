# Deprecations

This file tracks deprecated surfaces in Linchpin — what was deprecated, when, and when it will be removed.

Linchpin follows the deprecation policy described in the project's Release Plan: deprecated APIs continue to work for at least one minor release before removal, and every deprecation lands here with a target removal version.

## Active deprecations

### `vault_ids` field on `CreateSessionRequest`

- **Deprecated in:** v0.3.0
- **Targeted removal:** v0.4.0
- **Old shape:** `"vault_ids": ["v_1", "v_2"]` (top-level array)
- **New shape:** `"resources": [{"type": "vault", "vault_id": "v_1"}, ...]` — vault entries live in the unified `resources[]` array alongside `file` and `memory_store` resources.
- **Migration:** move each vault id into a `{"type": "vault", "vault_id": "..."}` entry inside `resources[]`. The legacy field continues to parse; requests using it get a `Linchpin-Deprecation: vault_ids` response header and a `session.deprecation_used` event.

### `permission_policy` as a flat string

- **Deprecated in:** v0.2.0
- **Targeted removal:** ~~v0.3.0~~ **v0.4.0** (extended — v0.3.0 shipped within the 60-day minimum-deprecation window of v0.2.0; removal moved forward one minor)
- **Old shape:** `"permission_policy": "always_allow"` (string)
- **New shape:** `"permission_policy": {"type": "always_allow"}` (discriminated object)
- **Migration:** wrap the string in `{"type": "..."}`. Either shape accepts input throughout v0.2.x and v0.3.x; v2 clients (sending `Linchpin-API-Version: 2026-05-13`) should send the object form.

### Flat `tools: [{type: "builtin", ...}]` list

- **Deprecated in:** v0.2.0
- **Targeted removal:** ~~v0.3.0~~ **v0.4.0** (extended — same reason as above)
- **Old shape:** `"tools": [{"type": "builtin", "default_config": {}, "configs": [...]}, {"type": "custom", ...}]`
- **New shape:** `"tools": {"type": "linchpin_toolset_20260512", "default_config": {}, "configs": [...]}` — built-in entries and custom-tool entries both live in the bundle's `configs[]`.
- **Migration:** wrap the flat list into the toolset bundle. Either shape accepts input throughout v0.2.x and v0.3.x.

### `session.requires_action` event

- **Deprecated in:** v0.2.0
- **Targeted removal:** ~~v0.3.0~~ **v0.4.0** (extended — same reason as above)
- **Old shape:** separate `session.requires_action` event signaling that the agent needs tool confirmation.
- **New shape:** `session.status_idle` event with a structured `stop_reason: {"type": "requires_action", "event_ids": [...]}`. The two-event sequence collapses into one.
- **Migration:** read `stop_reason` off the `session.status_idle` event.

### `agent.tool_use` for custom tools

- **Deprecated in:** v0.2.0
- **Targeted removal:** ~~v0.3.0~~ **v0.4.0** (extended — same reason as above)
- **Old shape:** `agent.tool_use` event for both built-in and custom-tool invocations.
- **New shape:** `agent.custom_tool_use` for custom HTTP tools; `agent.tool_use` retained for built-in tools.
- **Migration:** switch on the new event types if your client filters by tool kind.

## Removed

_Nothing has been removed yet._
