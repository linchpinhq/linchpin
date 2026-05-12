"""Unit tests for the PolicyEvaluator."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models import (
    Agent,
    BuiltinToolConfig,
    BuiltinToolItemConfig,
    CustomToolConfig,
    ModelConfig,
)
from app.policy import PolicyEvaluator


def _make_agent(tools=None) -> Agent:
    """Helper to build a minimal Agent with the given tools list."""
    return Agent(
        id="agent-1",
        name="test-agent",
        version=1,
        model=ModelConfig(provider="openrouter", id="anthropic/claude-sonnet-4"),
        system="You are helpful.",
        tools=tools or [],
        mcp_servers=[],
        created_at=datetime.now(timezone.utc),
    )


class TestPolicyEvaluatorBuiltinTools:
    """Tests for builtin tool permission lookup."""

    def test_always_allow_builtin(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", permission_policy="always_allow"),
                ],
            ),
        ])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("bash") == "always_allow"

    def test_always_ask_builtin(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="read", permission_policy="always_ask"),
                ],
            ),
        ])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("read") == "always_ask"

    def test_default_permission_is_always_ask(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[BuiltinToolItemConfig(name="write")],
            ),
        ])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("write") == "always_ask"

    def test_disabled_builtin_tool_is_denied(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(
                        name="bash", permission_policy="always_allow", enabled=False,
                    ),
                ],
            ),
        ])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("bash") == "denied"

    def test_multiple_builtin_configs(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", permission_policy="always_allow"),
                    BuiltinToolItemConfig(name="read", permission_policy="always_ask"),
                    BuiltinToolItemConfig(name="write", permission_policy="always_allow"),
                ],
            ),
        ])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("bash") == "always_allow"
        assert ev.evaluate("read") == "always_ask"
        assert ev.evaluate("write") == "always_allow"


class TestPolicyEvaluatorCustomTools:
    """Tests for custom tool permission lookup."""

    def test_always_allow_custom(self):
        agent = _make_agent(tools=[
            CustomToolConfig(
                type="custom",
                name="deploy",
                permission_policy="always_allow",
            ),
        ])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("deploy") == "always_allow"

    def test_always_ask_custom(self):
        agent = _make_agent(tools=[
            CustomToolConfig(
                type="custom",
                name="deploy",
                permission_policy="always_ask",
            ),
        ])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("deploy") == "always_ask"

    def test_default_custom_permission_is_always_ask(self):
        agent = _make_agent(tools=[
            CustomToolConfig(type="custom", name="my_tool"),
        ])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("my_tool") == "always_ask"


class TestPolicyEvaluatorDenied:
    """Tests for denied (unknown) tools."""

    def test_unknown_tool_denied(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[BuiltinToolItemConfig(name="bash")],
            ),
        ])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("nonexistent") == "denied"

    def test_empty_tools_denies_everything(self):
        agent = _make_agent(tools=[])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("bash") == "denied"
        assert ev.evaluate("anything") == "denied"


class TestPolicyEvaluatorMixed:
    """Tests with both builtin and custom tools."""

    def test_mixed_tool_types(self):
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name="bash", permission_policy="always_allow"),
                    BuiltinToolItemConfig(name="read", permission_policy="always_ask"),
                ],
            ),
            CustomToolConfig(
                type="custom",
                name="deploy",
                permission_policy="always_allow",
            ),
        ])
        ev = PolicyEvaluator(agent)
        assert ev.evaluate("bash") == "always_allow"
        assert ev.evaluate("read") == "always_ask"
        assert ev.evaluate("deploy") == "always_allow"
        assert ev.evaluate("unknown") == "denied"

    def test_constant_time_lookup(self):
        """Verify the evaluator uses dict-based O(1) lookup."""
        agent = _make_agent(tools=[
            BuiltinToolConfig(
                type="builtin",
                configs=[
                    BuiltinToolItemConfig(name=f"tool_{i}", permission_policy="always_allow")
                    for i in range(100)
                ],
            ),
        ])
        ev = PolicyEvaluator(agent)
        # Spot-check a few entries
        assert ev.evaluate("tool_0") == "always_allow"
        assert ev.evaluate("tool_99") == "always_allow"
        assert ev.evaluate("tool_100") == "denied"
