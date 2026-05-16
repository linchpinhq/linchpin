"""Tests for the Linchpin-API-Version negotiation (v0.2.0 breaking bundle).

Covers the middleware that resolves the header on every request, the
shape translators that convert between v1 and v2, and the per-session
persistence of the negotiated version.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.api_version import (
    HEADER_NAME,
    LATEST_VERSION,
    V1,
    V2_2026_05_13,
    permission_policy_v1_to_v2,
    permission_policy_v2_to_v1,
    tools_v1_to_v2,
    tools_v2_to_v1,
)

AUTH = {"Authorization": "Bearer test-secret-key"}


# ---- Header negotiation via the middleware ----


def test_no_header_echoes_v1(client):
    """Request without Linchpin-API-Version → response header echoes v1."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get(HEADER_NAME) == V1


def test_v2_header_echoes_v2(client):
    """Sending the dated header → response header echoes it back."""
    resp = client.get("/health", headers={HEADER_NAME: V2_2026_05_13})
    assert resp.status_code == 200
    assert resp.headers.get(HEADER_NAME) == V2_2026_05_13


def test_unknown_header_returns_400(client):
    """An unsupported date → 400 with the supported-versions list."""
    resp = client.get("/health", headers={HEADER_NAME: "2099-01-01"})
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["error"] == "unsupported_api_version"
    assert V2_2026_05_13 in body["supported_versions"]


def test_v2_header_with_whitespace_normalizes(client):
    """Leading/trailing whitespace tolerated."""
    resp = client.get("/health", headers={HEADER_NAME: f"  {V2_2026_05_13}  "})
    assert resp.status_code == 200
    assert resp.headers.get(HEADER_NAME) == V2_2026_05_13


def test_latest_version_is_the_v2_date():
    """LATEST_VERSION is exported as the canonical v2 date."""
    assert LATEST_VERSION == V2_2026_05_13


# ---- permission_policy translators ----


class TestPermissionPolicy:
    def test_v1_to_v2_string(self):
        assert permission_policy_v1_to_v2("always_allow") == {"type": "always_allow"}
        assert permission_policy_v1_to_v2("always_ask") == {"type": "always_ask"}

    def test_v1_to_v2_passthrough_dict(self):
        already_v2 = {"type": "always_allow"}
        assert permission_policy_v1_to_v2(already_v2) is already_v2

    def test_v2_to_v1_dict(self):
        assert permission_policy_v2_to_v1({"type": "always_allow"}) == "always_allow"
        assert permission_policy_v2_to_v1({"type": "always_ask"}) == "always_ask"

    def test_v2_to_v1_passthrough_string(self):
        assert permission_policy_v2_to_v1("always_allow") == "always_allow"

    def test_none_round_trips(self):
        assert permission_policy_v1_to_v2(None) is None
        assert permission_policy_v2_to_v1(None) is None


# ---- tools translators ----


class TestToolsTranslators:
    def test_v1_to_v2_builds_bundle(self):
        v1 = [
            {
                "type": "builtin",
                "default_config": {"timeout_ms": 30000},
                "configs": [
                    {"name": "bash", "permission_policy": "always_allow", "enabled": True},
                    {"name": "read", "permission_policy": "always_ask", "enabled": True},
                ],
            },
        ]
        v2 = tools_v1_to_v2(v1)
        assert v2["type"] == "linchpin_toolset_20260512"
        assert v2["default_config"] == {"timeout_ms": 30000}
        names = [c["name"] for c in v2["configs"]]
        assert names == ["bash", "read"]
        # permission_policy promoted to discriminated object
        assert v2["configs"][0]["permission_policy"] == {"type": "always_allow"}

    def test_v1_to_v2_includes_custom_tools(self):
        """Custom tools land in the bundle's `configs[]` alongside builtins."""
        v1 = [
            {"type": "builtin", "default_config": {}, "configs": [
                {"name": "bash", "permission_policy": "always_allow", "enabled": True},
            ]},
            {
                "type": "custom",
                "name": "gh_pr",
                "description": "Open PRs",
                "permission_policy": "always_ask",
                "endpoint": "https://example.com/hook",
                "input_schema": {},
            },
        ]
        v2 = tools_v1_to_v2(v1)
        types = [c.get("type", "builtin") for c in v2["configs"]]
        # Builtin entries are emitted as `type=builtin`, custom as `type=custom`
        assert "custom" in types
        custom_entry = next(c for c in v2["configs"] if c.get("type") == "custom")
        assert custom_entry["name"] == "gh_pr"
        assert custom_entry["permission_policy"] == {"type": "always_ask"}

    def test_v2_to_v1_unwraps_bundle(self):
        v2 = {
            "type": "linchpin_toolset_20260512",
            "default_config": {"timeout_ms": 5000},
            "configs": [
                {"name": "bash", "permission_policy": {"type": "always_allow"}, "enabled": True},
                {"type": "custom", "name": "gh_pr", "permission_policy": {"type": "always_ask"},
                 "endpoint": "https://example.com"},
            ],
        }
        v1 = tools_v2_to_v1(v2)
        # First entry: builtin with the configs[]
        builtin = next(t for t in v1 if t.get("type") == "builtin")
        assert builtin["default_config"] == {"timeout_ms": 5000}
        names = [c["name"] for c in builtin["configs"]]
        assert "bash" in names
        # permission_policy demoted to string
        assert builtin["configs"][0]["permission_policy"] == "always_allow"
        # Custom tool surfaces as its own entry
        custom = next(t for t in v1 if t.get("type") == "custom")
        assert custom["name"] == "gh_pr"
        assert custom["permission_policy"] == "always_ask"

    def test_round_trip_v1_v2_v1(self):
        original = [
            {"type": "builtin", "default_config": {"x": 1}, "configs": [
                {"name": "bash", "permission_policy": "always_allow", "enabled": True},
                {"name": "read", "permission_policy": "always_ask", "enabled": True},
            ]},
            {"type": "custom", "name": "gh", "description": "", "input_schema": {},
             "permission_policy": "always_ask", "endpoint": "https://e.com"},
        ]
        round_tripped = tools_v2_to_v1(tools_v1_to_v2(original))
        # Same set of tool names
        all_names_orig = sorted(
            [i["name"] for i in original[0]["configs"]] + [original[1]["name"]]
        )
        all_names_back = sorted(
            [i["name"] for i in round_tripped[0]["configs"]]
            + [t["name"] for t in round_tripped if t.get("type") == "custom"]
        )
        assert all_names_orig == all_names_back

    def test_v2_to_v1_passthrough_list(self):
        """If a v1 list is passed to v2→v1, return as-is."""
        v1 = [{"type": "builtin", "default_config": {}, "configs": []}]
        assert tools_v2_to_v1(v1) == v1


# ---- Pydantic model accepts both input shapes ----


class TestModelInputAcceptance:
    def test_create_agent_accepts_string_permission_policy(self, client):
        """v1 shape — permission_policy as a string — still works."""
        from unittest.mock import AsyncMock, patch

        payload = {
            "name": "v1-agent",
            "model": {"provider": "openrouter", "id": "claude-sonnet-4-6"},
            "system": "you are a test",
            "tools": [
                {
                    "type": "builtin",
                    "default_config": {},
                    "configs": [
                        {"name": "bash", "permission_policy": "always_allow"},
                    ],
                },
            ],
        }
        with patch("app.routes.agents.fetch_one", new_callable=AsyncMock) as f:
            f.return_value = _agent_row("v1-agent", payload["tools"])
            resp = client.post("/v1/agents", json=payload, headers=AUTH)
        assert resp.status_code == 201

    def test_create_agent_accepts_object_permission_policy(self, client):
        """v2 shape — permission_policy as `{type: ...}` — accepted, normalized."""
        from unittest.mock import AsyncMock, patch

        payload = {
            "name": "v2-agent",
            "model": {"provider": "openrouter", "id": "claude-sonnet-4-6"},
            "system": "you are a test",
            "tools": [
                {
                    "type": "builtin",
                    "default_config": {},
                    "configs": [
                        {"name": "bash", "permission_policy": {"type": "always_allow"}},
                    ],
                },
            ],
        }
        # Stored shape after validator runs is canonical v1 (string).
        canonical_tools = [
            {"type": "builtin", "default_config": {}, "configs": [
                {"name": "bash", "permission_policy": "always_allow", "enabled": True},
            ]},
        ]
        with patch("app.routes.agents.fetch_one", new_callable=AsyncMock) as f:
            f.return_value = _agent_row("v2-agent", canonical_tools)
            resp = client.post(
                "/v1/agents",
                json=payload,
                headers={**AUTH, HEADER_NAME: V2_2026_05_13},
            )
        assert resp.status_code == 201

    def test_create_agent_accepts_toolset_bundle(self, client):
        """v2 shape — `tools` as the toolset bundle — accepted, unwrapped."""
        from unittest.mock import AsyncMock, patch

        payload = {
            "name": "bundle-agent",
            "model": {"provider": "openrouter", "id": "claude-sonnet-4-6"},
            "system": "you are a test",
            "tools": {
                "type": "linchpin_toolset_20260512",
                "default_config": {},
                "configs": [
                    {"name": "bash", "permission_policy": {"type": "always_allow"}},
                ],
            },
        }
        canonical_tools = [
            {"type": "builtin", "default_config": {}, "configs": [
                {"name": "bash", "permission_policy": "always_allow", "enabled": True},
            ]},
        ]
        with patch("app.routes.agents.fetch_one", new_callable=AsyncMock) as f:
            f.return_value = _agent_row("bundle-agent", canonical_tools)
            resp = client.post(
                "/v1/agents",
                json=payload,
                headers={**AUTH, HEADER_NAME: V2_2026_05_13},
            )
        assert resp.status_code == 201

    def test_create_agent_rejects_invalid_permission_policy(self, client):
        """Random string for permission_policy → 422."""
        payload = {
            "name": "bad",
            "model": {"provider": "openrouter", "id": "claude-sonnet-4-6"},
            "system": "you are a test",
            "tools": [
                {
                    "type": "builtin",
                    "default_config": {},
                    "configs": [
                        {"name": "bash", "permission_policy": "totally_made_up"},
                    ],
                },
            ],
        }
        resp = client.post("/v1/agents", json=payload, headers=AUTH)
        assert resp.status_code == 422


def _agent_row(name: str, tools: list[dict]) -> dict:
    return {
        "id": uuid.uuid4(),
        "name": name,
        "version": 1,
        "model": {"provider": "openrouter", "id": "claude-sonnet-4-6"},
        "system": "you are a test",
        "tools": tools,
        "mcp_servers": [],
        "description": None,
        "metadata": {},
        "archived_at": None,
        "created_at": datetime(2026, 5, 15, tzinfo=timezone.utc),
    }
