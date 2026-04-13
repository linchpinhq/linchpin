"""Policy evaluator for tool permission enforcement.

Evaluates per-tool permission policies configured on an Agent to determine
whether a tool call should be auto-approved, require user confirmation,
or be denied outright.

Requirements: 15.1, 15.2, 15.3, 15.4
"""

from __future__ import annotations

from typing import Literal

from app.models import Agent, BuiltinToolConfig, CustomToolConfig

Permission = Literal["always_allow", "always_ask", "denied"]


class PolicyEvaluator:
    """Dict-based permission lookup for an agent's configured tools.

    Builds an O(1) lookup table from the agent's tool list at construction
    time.  Each tool name maps to its configured permission policy
    (``always_allow`` or ``always_ask``).  Tools not present in the agent's
    list are denied.
    """

    def __init__(self, agent: Agent) -> None:
        self._permissions: dict[str, Permission] = {}
        self._build_lookup(agent)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, tool_name: str) -> Permission:
        """Return the permission for *tool_name*.

        Returns ``"always_allow"`` or ``"always_ask"`` for known tools,
        ``"denied"`` for any tool not in the agent's configured list.
        """
        return self._permissions.get(tool_name, "denied")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_lookup(self, agent: Agent) -> None:
        """Construct the permission dict from the agent's tools list."""
        for tool in agent.tools:
            if isinstance(tool, BuiltinToolConfig):
                for cfg in tool.configs:
                    if cfg.enabled:
                        self._permissions[cfg.name] = cfg.permission_policy
            elif isinstance(tool, CustomToolConfig):
                self._permissions[tool.name] = tool.permission_policy
