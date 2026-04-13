# Bugfix Requirements Document

## Introduction

MCP tools configured on an agent (via `mcp_servers`) are never discovered or advertised to the model. The orchestrator's `build_tool_definitions()` only iterates over `agent.tools` (built-in and custom tool configs), completely ignoring `agent.mcp_servers`. As a result, the model never knows MCP tools exist, cannot call them, and the entire MCP integration specified in Requirement 16 of the linchpin-mvp spec is non-functional.

Additionally, the connector's `MCPManager` has `start_server()` and `invoke()` methods but no `tools/list` discovery capability, and the connector never spawns MCP server subprocesses at session start. The orchestrator has no mechanism to ask the connector what tools an MCP server offers.

This affects any agent configured with MCP servers — for example, a "Customer support agent" with a Notion MCP server will never be able to use Notion tools because the model is never told they exist.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN an agent is configured with `mcp_servers` entries AND a session starts, THEN the system does not spawn MCP server subprocesses and does not call `tools/list` to discover available tools from those servers.

1.2 WHEN the orchestrator calls `build_tool_definitions(agent)` to construct the tool list for the model, THEN the system only includes tools from `agent.tools` (BuiltinToolConfig and CustomToolConfig entries) and omits all MCP server tools from `agent.mcp_servers`.

1.3 WHEN the model receives the tool definitions list, THEN the model has no knowledge of any MCP tools and therefore never generates `tool_use` calls targeting MCP tools.

1.4 WHEN the connector receives an MCP tool invocation via `POST /tools/invoke`, THEN the `MCPManager.invoke()` method returns an error because no MCP server subprocess was ever started for the session (the server is "not found").

1.5 WHEN the connector's `handle_mcp_invoke()` function processes a request with credentials and environment variables, THEN the resolved `env_vars` are computed but never passed to `mcp_manager.start_server()` or `mcp_manager.invoke()`, so MCP servers cannot authenticate with external services.

### Expected Behavior (Correct)

2.1 WHEN an agent is configured with `mcp_servers` entries AND a session starts, THEN the connector SHALL spawn each MCP server as a subprocess using stdio transport and call `tools/list` on each to discover available tools.

2.2 WHEN the orchestrator calls `build_tool_definitions(agent)`, THEN the system SHALL include MCP tools in the returned list, using the `server_name__tool_name` naming convention, alongside built-in and custom tool definitions.

2.3 WHEN the model receives the tool definitions list, THEN the model SHALL see all MCP tools with their names, descriptions, and input schemas, enabling it to generate `tool_use` calls for MCP tools.

2.4 WHEN the connector receives an MCP tool invocation via `POST /tools/invoke`, THEN the `MCPManager` SHALL have a running subprocess for the specified server and SHALL forward the call to it and return the result.

2.5 WHEN the connector spawns an MCP server subprocess, THEN the connector SHALL pass the configured environment variables (including resolved credentials) to the subprocess so that MCP servers can authenticate with external services.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN an agent has no `mcp_servers` configured, THEN the system SHALL CONTINUE TO build tool definitions from `agent.tools` only and operate normally without any MCP-related overhead.

3.2 WHEN the orchestrator receives a tool call for a built-in tool (bash, read, write, edit, glob, grep, web_fetch, web_search), THEN the system SHALL CONTINUE TO execute it via the sandbox and return results as before.

3.3 WHEN the orchestrator receives a tool call for a custom HTTP tool, THEN the system SHALL CONTINUE TO forward it to the connector's `POST /tools/invoke` endpoint with `tool_type: "custom_http"` and return results as before.

3.4 WHEN `classify_tool()` is called with a tool name matching the `server_name__tool_name` pattern, THEN the system SHALL CONTINUE TO classify it as `"mcp"` and route it through the MCP invocation path.

3.5 WHEN `dispatch_tool()` processes an MCP tool call, THEN the system SHALL CONTINUE TO emit `agent.mcp_tool_result` events with the tool output.

3.6 WHEN the connector's `MCPManager.stop_all()` is called for a session, THEN the system SHALL CONTINUE TO terminate all MCP server subprocesses for that session.
