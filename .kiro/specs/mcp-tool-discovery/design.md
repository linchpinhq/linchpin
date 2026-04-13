# MCP Tool Discovery Bugfix Design

## Overview

MCP tools configured on an agent via `mcp_servers` are never discovered or advertised to the LLM. The orchestrator's `build_tool_definitions()` only iterates over `agent.tools` (built-in and custom), completely ignoring `agent.mcp_servers`. The connector has no `tools/list` endpoint to discover what tools an MCP server offers. As a result, the model never knows MCP tools exist and cannot call them.

The fix adds a `POST /tools/list` endpoint to the connector that spawns an MCP server subprocess and queries its available tools, then modifies `build_tool_definitions()` in the orchestrator to call this endpoint for each MCP server and include the discovered tools (prefixed with `server_name__`) in the tool definitions sent to the model.

## Glossary

- **Bug_Condition (C)**: An agent has one or more `mcp_servers` entries — the model never receives tool definitions for those MCP tools
- **Property (P)**: When an agent has `mcp_servers`, `build_tool_definitions()` returns MCP tool definitions (name, description, input_schema) alongside built-in and custom tools
- **Preservation**: Built-in tool definitions, custom tool definitions, `classify_tool()` routing, `dispatch_tool()` MCP event emission, and `MCPManager.stop_all()` must remain unchanged
- **`build_tool_definitions()`**: The function in `linchpin-api/app/orchestrator.py` that constructs the tool list sent to the model provider
- **`MCPManager`**: The class in `linchpin-connector/app/mcp.py` that manages MCP server subprocesses per session via stdio transport
- **`POST /tools/invoke`**: The existing connector endpoint that forwards tool calls to MCP servers or HTTP endpoints
- **`server_name__tool_name`**: The naming convention used to namespace MCP tools (e.g., `notion__search`)

## Bug Details

### Bug Condition

The bug manifests when an agent is configured with `mcp_servers` entries and a session starts. The orchestrator's `build_tool_definitions()` only processes `agent.tools` (BuiltinToolConfig and CustomToolConfig), never queries MCP servers for their available tools, and therefore the model receives no MCP tool definitions.

Additionally, the connector has no endpoint to discover MCP tools — `MCPManager` only has `start_server()` and `invoke()`, with no `list_tools()` capability. Even if the orchestrator tried to discover tools, there is no API to call.

**Formal Specification:**
```
FUNCTION isBugCondition(agent, session)
  INPUT: agent of type Agent, session of type Session
  OUTPUT: boolean

  RETURN len(agent.mcp_servers) > 0
         AND build_tool_definitions(agent) does NOT contain any tool
             with name matching "server_name__*" for any server in agent.mcp_servers
END FUNCTION
```

### Examples

- Agent with `mcp_servers: [{name: "notion", command: "npx", args: ["-y", "@notionhq/mcp"]}]` — `build_tool_definitions()` returns only built-in tools, no `notion__search`, `notion__create_page`, etc.
- Agent with both `tools` (bash, read) and `mcp_servers` (notion) — model sees bash and read but not notion tools
- Agent with two MCP servers (notion, github) — model sees zero MCP tools from either server
- Agent with `mcp_servers: []` (empty list) — no bug, `build_tool_definitions()` correctly returns only `agent.tools` (edge case, not affected)

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Agents with no `mcp_servers` must continue to have tool definitions built from `agent.tools` only, with no MCP-related overhead or connector calls
- Built-in tool execution (bash, read, write, edit, glob, grep, web_fetch, web_search) via the sandbox must continue to work as before
- Custom HTTP tool forwarding to `POST /tools/invoke` with `tool_type: "custom_http"` must continue to work as before
- `classify_tool()` must continue to classify `server_name__tool_name` patterns as `"mcp"` and route through the MCP invocation path
- `dispatch_tool()` must continue to emit `agent.mcp_tool_result` events for MCP tool calls
- `MCPManager.stop_all()` must continue to terminate all MCP server subprocesses for a session

**Scope:**
All inputs that do NOT involve agents with `mcp_servers` entries should be completely unaffected by this fix. This includes:
- Sessions for agents with only built-in tools
- Sessions for agents with only custom HTTP tools
- The existing `POST /tools/invoke` endpoint behavior
- Event emission, context building, and state machine transitions

## Hypothesized Root Cause

Based on the bug description and code analysis, the root causes are:

1. **Missing `tools/list` endpoint on connector**: `MCPManager` has `start_server()` and `invoke()` but no method to call the MCP protocol's `tools/list` method. The connector's `main.py` only exposes `POST /tools/invoke` — there is no `POST /tools/list` endpoint. Without this, the orchestrator has no way to discover what tools an MCP server offers.

2. **`build_tool_definitions()` ignores `agent.mcp_servers`**: The function in `orchestrator.py` only iterates over `agent.tools`, processing `BuiltinToolConfig` and `CustomToolConfig` entries. It never looks at `agent.mcp_servers` and never calls the connector to discover MCP tools.

3. **MCP server not started before discovery**: The `handle_mcp_invoke()` function in the connector computes `env_vars` from credentials but never passes them to `mcp_manager.start_server()`. The server is never spawned, so even `invoke()` fails with "server not found". The `tools/list` flow needs to start the server as a prerequisite.

4. **No `list_tools()` method on `MCPManager`**: The class can start a server and invoke a tool, but has no method to send a `tools/list` JSON-RPC request over stdio and parse the response. This is the missing primitive that the new endpoint needs.

## Correctness Properties

Property 1: Bug Condition - MCP Tools Included in Tool Definitions

_For any_ agent where `len(agent.mcp_servers) > 0` and each MCP server responds to `tools/list` with a non-empty tool list, the fixed `build_tool_definitions()` function SHALL return tool definitions that include each MCP tool prefixed with `server_name__`, with the tool's description and input_schema from the MCP server's response.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - Non-MCP Tool Definitions Unchanged

_For any_ agent (regardless of `mcp_servers` configuration), the fixed `build_tool_definitions()` function SHALL produce the same built-in and custom tool definitions as the original function, preserving all existing tool names, descriptions, and input schemas for non-MCP tools.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

## Fix Implementation

### Changes Required

Assuming our root cause analysis is correct:

**File**: `linchpin-connector/app/mcp.py`

**Class**: `MCPManager`

**Specific Changes**:
1. **Add `list_tools()` method**: Send a `tools/list` JSON-RPC request over stdio to a running MCP server subprocess and parse the response. This mirrors the existing `invoke()` method but uses the `tools/list` method instead of `tools/call`. The method should start the server if not already running (accepting `command`, `args`, `env` parameters to pass through to `start_server()`).

---

**File**: `linchpin-connector/app/main.py`

**Endpoint**: New `POST /tools/list`

**Specific Changes**:
2. **Add `ToolListRequest` model**: Pydantic model accepting `session_id`, `server_name`, `command`, `args`, `env` fields.
3. **Add `ToolListResponse` model**: Pydantic model returning `tools` (list of dicts with `name`, `description`, `input_schema`) and optional `error`.
4. **Add `POST /tools/list` endpoint**: Accepts a `ToolListRequest`, calls `mcp_manager.start_server()` with the provided command/args/env, then calls `mcp_manager.list_tools()`, and returns the discovered tools. Handles errors (server crash, timeout, invalid JSON) gracefully.

---

**File**: `linchpin-api/app/orchestrator.py`

**Function**: `build_tool_definitions()`

**Specific Changes**:
5. **Make `build_tool_definitions()` async**: The function needs to call the connector's HTTP endpoint, so it must become `async def build_tool_definitions(agent, session_id)`.
6. **Add MCP tool discovery loop**: For each entry in `agent.mcp_servers`, call `POST /tools/list` on the connector with `session_id`, `server_name`, `command`, `args`, and `env`. For each tool returned, create a tool definition with name `f"{server.name}__{tool.name}"`, the tool's description, and the tool's input_schema.
7. **Update call site**: The call to `build_tool_definitions(agent)` in `run_session()` must be updated to `await build_tool_definitions(agent, session_id)`.

---

**File**: `linchpin-connector/app/main.py`

**Function**: `handle_mcp_invoke()`

**Specific Changes**:
8. **Pass env_vars to `start_server()`**: The existing `handle_mcp_invoke()` computes `env_vars` but never uses them. Before calling `mcp_manager.invoke()`, call `mcp_manager.start_server()` with the resolved `env_vars` so the server is running. This requires the request to include `command` and `args` or the server to already be running from a prior `tools/list` call.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on unfixed code, then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix. Confirm or refute the root cause analysis. If we refute, we will need to re-hypothesize.

**Test Plan**: Write tests that create an agent with `mcp_servers` entries and call `build_tool_definitions()`, asserting that MCP tools appear in the result. Run these tests on the UNFIXED code to observe failures and confirm the root cause.

**Test Cases**:
1. **Missing MCP tools test**: Create agent with one MCP server, call `build_tool_definitions()` — assert MCP tools are present (will fail on unfixed code because the function ignores `mcp_servers`)
2. **No tools/list endpoint test**: Send `POST /tools/list` to the connector — assert 200 response (will fail on unfixed code because the endpoint doesn't exist)
3. **No list_tools method test**: Call `mcp_manager.list_tools()` — assert it returns tools (will fail on unfixed code because the method doesn't exist)
4. **Server not started on invoke test**: Call `POST /tools/invoke` for an MCP tool without prior `tools/list` — assert server is running (will fail on unfixed code because server is never started)

**Expected Counterexamples**:
- `build_tool_definitions()` returns zero MCP tools for an agent with `mcp_servers` entries
- Connector returns 404 for `POST /tools/list`
- `MCPManager` has no `list_tools` attribute
- Possible causes: `build_tool_definitions()` only iterates `agent.tools`, no `tools/list` endpoint, no `list_tools()` method

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL agent WHERE len(agent.mcp_servers) > 0 DO
  tools := await build_tool_definitions_fixed(agent, session_id)
  FOR EACH server IN agent.mcp_servers DO
    mcp_tools := tools_from_server(server)
    FOR EACH mcp_tool IN mcp_tools DO
      ASSERT f"{server.name}__{mcp_tool.name}" IN [t["name"] FOR t IN tools]
      ASSERT tool_has_description(mcp_tool)
      ASSERT tool_has_input_schema(mcp_tool)
    END FOR
  END FOR
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL agent WHERE len(agent.mcp_servers) == 0 DO
  ASSERT build_tool_definitions_original(agent) == await build_tool_definitions_fixed(agent, session_id)
END FOR

FOR ALL agent, tool_name WHERE classify_tool(tool_name, agent) == "builtin" DO
  ASSERT dispatch_tool_original(tool_name) == dispatch_tool_fixed(tool_name)
END FOR

FOR ALL agent, tool_name WHERE classify_tool(tool_name, agent) == "custom_http" DO
  ASSERT dispatch_tool_original(tool_name) == dispatch_tool_fixed(tool_name)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many agent configurations automatically across the input domain
- It catches edge cases like agents with empty tool lists, mixed tool types, or unusual MCP server names
- It provides strong guarantees that non-MCP behavior is unchanged

**Test Plan**: Observe behavior on UNFIXED code first for agents without MCP servers, then write property-based tests capturing that behavior.

**Test Cases**:
1. **Built-in tool definitions preservation**: Create agents with various built-in tool configs, verify `build_tool_definitions()` returns identical results before and after fix
2. **Custom tool definitions preservation**: Create agents with custom HTTP tools, verify definitions are unchanged
3. **classify_tool preservation**: Verify `classify_tool()` returns the same results for all tool name patterns
4. **dispatch_tool preservation**: Verify built-in and custom HTTP tool dispatch produces the same results

### Unit Tests

- Test `MCPManager.list_tools()` with a mock subprocess that returns valid `tools/list` responses
- Test `MCPManager.list_tools()` with subprocess crash, timeout, and invalid JSON
- Test `POST /tools/list` endpoint with valid request, missing server_name, subprocess errors
- Test `build_tool_definitions()` with agents that have 0, 1, and multiple MCP servers
- Test `build_tool_definitions()` with agents that have both `tools` and `mcp_servers`
- Test tool name prefixing: `server.name + "__" + tool.name` for various server/tool name combinations
- Test `handle_mcp_invoke()` now starts server before invoking

### Property-Based Tests

- Generate random agent configurations with varying `mcp_servers` lists and verify all MCP tools appear in `build_tool_definitions()` output with correct prefixes
- Generate random agent configurations with no `mcp_servers` and verify `build_tool_definitions()` output matches the original function exactly
- Generate random tool names and verify `classify_tool()` produces identical results before and after fix

### Integration Tests

- End-to-end: create agent with MCP server → create session → verify model receives MCP tool definitions → model calls MCP tool → verify result
- Test connector `POST /tools/list` → `POST /tools/invoke` flow: list tools first, then invoke one
- Test that MCP server subprocess is started once and reused across `tools/list` and subsequent `tools/invoke` calls
- Test multiple MCP servers on one agent: verify all servers' tools are discovered and namespaced correctly
