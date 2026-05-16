"""Wire-format version negotiation (v0.2.0 breaking bundle).

The package version (``0.x.y``) is what self-hosters install. The wire
format is independently versioned by a dated header: ``Linchpin-API-Version``.

| Header value | Wire shape | Note |
|---|---|---|
| absent | v1 (legacy) | session.requires_action, flat tools list, string permission_policy, agent.tool_use for custom tools |
| ``2026-05-13`` | v2 | session.status_idle.stop_reason structured, toolset bundle, discriminated permission_policy, agent.custom_tool_use |
| anything else | 400 | unknown version → reject with the list of supported versions |

Three downstream concerns consume the negotiated version:

1. **Request body normalization.** Routes accept both v1 and v2 input
   shapes regardless of the header — converting v2 → v1 at validation
   so canonical storage stays v1-shaped (no schema migration needed).
   When v1 input is received against the v2 header (mismatch), the
   ``Linchpin-Deprecation`` response header is set.

2. **Response serialization.** Routes serialize outputs in the
   negotiated version. v1 = legacy shape; v2 = new shape converted on
   the fly from the canonical v1 stored form.

3. **Session-scoped reads (events / SSE / session-get).** A session
   stores the version it was created with in ``sessions.linchpin_api_version``.
   Subsequent reads of that session's events keep the shape the session
   booted with, so a long-running v1-shape client doesn't see its event
   stream change mid-stream.
"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, Request, Response

# Cobalt-blue line: the only currently-supported dated wire version.
# Adding a new version here is the *only* place new dates get registered.
V1: Literal["v1"] = "v1"
V2_2026_05_13: Literal["2026-05-13"] = "2026-05-13"

ApiVersion = Literal["v1", "2026-05-13"]

SUPPORTED_VERSIONS: tuple[str, ...] = (V2_2026_05_13,)
LATEST_VERSION: str = V2_2026_05_13

HEADER_NAME = "Linchpin-API-Version"
DEPRECATION_HEADER = "Linchpin-Deprecation"


def negotiate(request: Request) -> ApiVersion:
    """Parse the ``Linchpin-API-Version`` header and return the chosen version.

    No header → ``"v1"`` (legacy). Known date → that date. Unknown date →
    400 with the supported list so clients see a clean failure rather
    than silently getting served the wrong shape.

    The result is cached on ``request.state.api_version`` so multiple
    dependencies don't re-parse.
    """
    cached = getattr(request.state, "api_version", None)
    if cached is not None:
        return cached

    raw = request.headers.get(HEADER_NAME)
    if not raw:
        request.state.api_version = V1
        return V1

    raw = raw.strip()
    if raw in SUPPORTED_VERSIONS:
        request.state.api_version = raw
        return raw  # type: ignore[return-value]

    raise HTTPException(
        status_code=400,
        detail={
            "error": "unsupported_api_version",
            "message": (
                f"{HEADER_NAME}: {raw!r} is not a known wire-format version. "
                f"Supported: {list(SUPPORTED_VERSIONS)}, or omit for legacy v1."
            ),
            "supported_versions": list(SUPPORTED_VERSIONS),
        },
    )


def echo_version_header(response: Response, version: ApiVersion) -> None:
    """Set the ``Linchpin-API-Version`` response header to the active version.

    Even for v1 (header absent on request) we echo back the resolved name
    so clients can tell whether the server understood their request.
    """
    response.headers[HEADER_NAME] = version


def mark_deprecation(response: Response, shape: str) -> None:
    """Set the ``Linchpin-Deprecation`` response header.

    Called when an incoming request used a shape that's slated for removal
    in v0.3.0. Multiple deprecations comma-join into one header value.
    """
    existing = response.headers.get(DEPRECATION_HEADER, "")
    if existing:
        response.headers[DEPRECATION_HEADER] = f"{existing}, {shape}"
    else:
        response.headers[DEPRECATION_HEADER] = shape


# ---------------------------------------------------------------------------
# Shape translators
# ---------------------------------------------------------------------------
# Canonical storage stays v1-shaped (no schema migration). These helpers
# convert canonical (v1) → v2 on output and v2 → canonical (v1) on input.

# Built-in tools that count as "custom" for event-naming purposes.
# Currently empty — see notes in the orchestrator's emit code.

DEPRECATED_AGENT_TOOLS_FLAT_LIST = "tools.flat-list"
DEPRECATED_AGENT_PERMISSION_POLICY_STRING = "permission_policy.string"
DEPRECATED_SESSION_REQUIRES_ACTION_EVENT = "session.requires_action.legacy"


def permission_policy_v1_to_v2(value: str | dict | None) -> dict | None:
    """``"always_allow"`` → ``{"type": "always_allow"}``. Pass-through for dicts."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value  # already v2
    return {"type": value}


def permission_policy_v2_to_v1(value: str | dict | None) -> str | None:
    """``{"type": "always_allow"}`` → ``"always_allow"``."""
    if value is None:
        return None
    if isinstance(value, str):
        return value  # already v1
    return value.get("type")


def tools_v1_to_v2(tools: list[dict]) -> dict:
    """Wrap the v1 flat tools list into the v2 ``linchpin_toolset_20260512``
    bundle. Builtin entries collapse into the bundle's `configs[]`; custom
    tools are emitted as `{type: "custom", ...}` entries inside `configs[]`
    so SDKs see one unified list. Permission policies inside configs are
    promoted to the discriminated object form.
    """
    default_config: dict = {}
    configs: list[dict] = []
    for tool in tools:
        ttype = tool.get("type")
        if ttype == "builtin":
            # Builtin entries already have default_config + configs[]; merge
            # the first one's default_config into the top-level (rare for
            # multiple builtin entries to exist; defensive merge anyway).
            for k, v in (tool.get("default_config") or {}).items():
                default_config.setdefault(k, v)
            for item in tool.get("configs") or []:
                item_v2 = dict(item)
                item_v2["type"] = "builtin"
                if "permission_policy" in item_v2:
                    item_v2["permission_policy"] = permission_policy_v1_to_v2(
                        item_v2["permission_policy"]
                    )
                configs.append(item_v2)
        elif ttype == "custom":
            entry = dict(tool)
            if "permission_policy" in entry:
                entry["permission_policy"] = permission_policy_v1_to_v2(
                    entry["permission_policy"]
                )
            configs.append(entry)
        else:
            # Unknown tool type — preserve verbatim so v2 clients see what's
            # there. Server validation has already passed at this point.
            configs.append(tool)
    return {
        "type": "linchpin_toolset_20260512",
        "default_config": default_config,
        "configs": configs,
    }


def tools_v2_to_v1(bundle: dict | list) -> list[dict]:
    """Convert a v2 toolset bundle back to the v1 flat list shape.

    Pass-through if ``bundle`` is already a list (a v1 client that hits a
    code path expecting either shape).
    """
    if isinstance(bundle, list):
        return bundle  # already v1
    if not isinstance(bundle, dict):
        return []
    bundle_default = bundle.get("default_config") or {}
    configs = bundle.get("configs") or []

    builtin_items: list[dict] = []
    custom_items: list[dict] = []
    for entry in configs:
        etype = entry.get("type", "builtin")  # default to builtin for v0.1 shapes
        if etype == "custom":
            v1_entry = dict(entry)
            if "permission_policy" in v1_entry:
                v1_entry["permission_policy"] = permission_policy_v2_to_v1(
                    v1_entry["permission_policy"]
                )
            custom_items.append(v1_entry)
        else:
            v1_item = {k: v for k, v in entry.items() if k != "type"}
            if "permission_policy" in v1_item:
                v1_item["permission_policy"] = permission_policy_v2_to_v1(
                    v1_item["permission_policy"]
                )
            builtin_items.append(v1_item)

    out: list[dict] = []
    if builtin_items or bundle_default:
        out.append({
            "type": "builtin",
            "default_config": bundle_default,
            "configs": builtin_items,
        })
    out.extend(custom_items)
    return out
