"""Tests for v0.5.0 PR2 — MCP server discriminator.

The legacy stdio shape (no ``type`` key) still validates; the new
``type: "url"`` variant accepts the URL form; mixed lists work.
"""

from __future__ import annotations

import pytest

from app.models import (
    CreateAgentRequest,
    MCPServerConfig,
    StdioMCPServerConfig,
    UrlMCPServerConfig,
)
from pydantic import TypeAdapter, ValidationError


_BASE_AGENT = {
    "name": "tester",
    "model": {"provider": "openrouter", "id": "claude", "base_url": None},
    "system": "you are helpful",
    "tools": [],
}


def test_legacy_stdio_without_type_still_parses():
    req = CreateAgentRequest.model_validate({
        **_BASE_AGENT,
        "mcp_servers": [
            {"name": "fs", "command": "npx", "args": ["@modelcontextprotocol/server-filesystem"]},
        ],
    })
    assert isinstance(req.mcp_servers[0], StdioMCPServerConfig)
    assert req.mcp_servers[0].command == "npx"


def test_explicit_stdio_parses():
    req = CreateAgentRequest.model_validate({
        **_BASE_AGENT,
        "mcp_servers": [
            {"type": "stdio", "name": "fs", "command": "npx", "args": ["@x"]},
        ],
    })
    assert isinstance(req.mcp_servers[0], StdioMCPServerConfig)


def test_url_variant_parses():
    req = CreateAgentRequest.model_validate({
        **_BASE_AGENT,
        "mcp_servers": [
            {
                "type": "url",
                "name": "gh-copilot",
                "url": "https://api.githubcopilot.com/mcp/",
                "vault_ids": ["vault_xyz"],
            },
        ],
    })
    assert isinstance(req.mcp_servers[0], UrlMCPServerConfig)
    assert req.mcp_servers[0].vault_ids == ["vault_xyz"]


def test_mixed_list_parses():
    req = CreateAgentRequest.model_validate({
        **_BASE_AGENT,
        "mcp_servers": [
            {"name": "fs", "command": "npx", "args": []},
            {"type": "url", "name": "linear", "url": "https://linear/mcp", "vault_ids": ["v"]},
        ],
    })
    assert isinstance(req.mcp_servers[0], StdioMCPServerConfig)
    assert isinstance(req.mcp_servers[1], UrlMCPServerConfig)


def test_url_missing_url_rejected():
    with pytest.raises(ValidationError):
        CreateAgentRequest.model_validate({
            **_BASE_AGENT,
            "mcp_servers": [
                {"type": "url", "name": "x", "vault_ids": []},
            ],
        })


def test_stdio_missing_command_rejected():
    with pytest.raises(ValidationError):
        CreateAgentRequest.model_validate({
            **_BASE_AGENT,
            "mcp_servers": [{"type": "stdio", "name": "x"}],
        })


def test_adapter_round_trip_preserves_type_field():
    """A persisted row that round-trips through TypeAdapter keeps its
    discriminator — important for ``GET /v1/agents`` serializing back
    to the wire shape."""
    adapter = TypeAdapter(MCPServerConfig)
    stdio = adapter.validate_python(
        {"type": "stdio", "name": "fs", "command": "npx"}
    )
    url = adapter.validate_python(
        {"type": "url", "name": "gh", "url": "https://x/mcp", "vault_ids": []}
    )
    assert adapter.dump_python(stdio)["type"] == "stdio"
    assert adapter.dump_python(url)["type"] == "url"


def test_url_variant_default_vault_ids_empty():
    req = CreateAgentRequest.model_validate({
        **_BASE_AGENT,
        "mcp_servers": [
            {"type": "url", "name": "gh", "url": "https://x/mcp"},
        ],
    })
    assert req.mcp_servers[0].vault_ids == []
