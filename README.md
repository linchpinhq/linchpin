# Linchpin

Self-hostable runtime for managed AI agents. Run agents on your own infrastructure, with any model provider, no vendor lock-in.

Linchpin gives you an API to create agents, run them inside isolated Docker containers, stream events in real time via SSE, and execute tools with configurable permissions.

## Architecture

```
┌─────────┐       HTTP + SSE        ┌──────────────┐
│  Client  │ ◄────────────────────► │ linchpin-api │
└─────────┘                         │  (FastAPI)   │
                                    └──────┬───────┘
                                           │
                    ┌──────────────┬────────┼────────────┐
                    │              │        │            │
               ┌────▼────┐  ┌─────▼──┐  ┌──▼───┐  ┌────▼─────────┐
               │ Postgres │  │ Docker │  │ LLM  │  │  linchpin-   │
               │   16     │  │ Socket │  │ APIs │  │  connector   │
               └──────────┘  └────────┘  └──────┘  └──────────────┘
                                                      │         │
                                                 MCP servers  HTTP tools
```

Two Python 3.12 services plus Postgres 16, deployed with a single `docker-compose up`.

- **linchpin-api** — HTTP surface, orchestrator loop, Docker sandbox, built-in tools, SSE streaming
- **linchpin-connector** — MCP server management (stdio transport), custom HTTP tool invocation
- **Postgres 16** — persistent storage, Alembic migrations, LISTEN/NOTIFY for event fanout

## Quick Start

```bash
# Clone and start
git clone <repo-url> && cd linchpin
docker-compose up --build
```

The API is available at `http://localhost:8000`. Default API key: `changeme`.

Set a real key:

```bash
LINCHPIN_API_KEY=your-secret-key docker-compose up --build
```

## API

All endpoints require a bearer token in the `Authorization` header:

```
Authorization: Bearer <LINCHPIN_API_KEY>
```

### Agents

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/agents` | Create an agent |
| GET | `/v1/agents/{id}` | Get an agent |
| GET | `/v1/agents` | List agents (paginated) |

### Environments

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/environments` | Create an environment |
| GET | `/v1/environments/{id}` | Get an environment |
| GET | `/v1/environments` | List environments (paginated) |

### Sessions

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/sessions` | Create a session |
| GET | `/v1/sessions` | List sessions (paginated) |
| GET | `/v1/sessions/{id}` | Get a session |
| DELETE | `/v1/sessions/{id}` | Terminate a session |
| POST | `/v1/sessions/{id}/archive` | Archive a session |
| POST | `/v1/sessions/{id}/events` | Post events to a session |
| GET | `/v1/sessions/{id}/events` | Get events (cursor-paginated) |
| GET | `/v1/sessions/{id}/stream` | SSE event stream |

### Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (no auth) |

## Usage Example

```bash
API=http://localhost:8000
KEY=changeme

# Create an agent
curl -s -X POST "$API/v1/agents" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-agent",
    "model": {"provider": "anthropic", "id": "claude-sonnet-4-20250514"},
    "system": "You are a helpful assistant.",
    "tools": [
      {"type": "builtin", "configs": [{"name": "bash", "permission_policy": "always_allow"}]}
    ]
  }' | jq .

# Create an environment
curl -s -X POST "$API/v1/environments" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "sandbox",
    "config": {"networking": {"type": "none"}}
  }' | jq .

# Create a session (uses agent_id and environment_id from above)
curl -s -X POST "$API/v1/sessions" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "<agent-id>",
    "environment_id": "<environment-id>"
  }' | jq .

# Send a message
curl -s -X POST "$API/v1/sessions/<session-id>/events" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "events": [{"type": "user.message", "payload": {"content": "Hello!"}}]
  }' | jq .

# Stream events (SSE)
curl -N "$API/v1/sessions/<session-id>/stream" \
  -H "Authorization: Bearer $KEY"
```

## Model Providers

Linchpin supports three providers out of the box:

| Provider | Config |
|----------|--------|
| Anthropic | `{"provider": "anthropic", "id": "claude-sonnet-4-20250514"}` |
| OpenAI | `{"provider": "openai", "id": "gpt-4o"}` |
| Ollama | `{"provider": "ollama", "id": "llama3", "base_url": "http://host:11434"}` |

Set the appropriate API key as an environment variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) for the provider you're using.

## Built-in Tools

Agents have access to 8 built-in tools that execute inside the session's Docker container:

| Tool | Description |
|------|-------------|
| `bash` | Run shell commands |
| `read` | Read a file |
| `write` | Write a file |
| `edit` | Apply edits to a file |
| `glob` | Find files by pattern |
| `grep` | Search file contents |
| `web_fetch` | Fetch a URL |
| `web_search` | Web search (stub) |

## Networking

Each environment configures container networking:

- **`none`** — Container attached to `linchpin-none` network, no external access
- **`unrestricted`** — Container attached to `linchpin-open` network, full external access

## Tool Permissions

Each tool can be configured with a permission policy:

- **`always_allow`** — Tool executes immediately without confirmation
- **`always_ask`** — Emits `session.requires_action` and waits for `user.tool_confirmation`

## MCP & Custom Tools

Agents can use external tools via:

- **MCP servers** — Configured per-agent, spawned as subprocesses with stdio transport by linchpin-connector
- **Custom HTTP tools** — Forward tool calls to user-defined HTTP endpoints

## Development

```bash
# Install dependencies
cd linchpin-api && pip install -e ".[dev]"
cd ../linchpin-connector && pip install -e ".[dev]"

# Run tests
cd linchpin-api && pytest
cd ../linchpin-connector && pytest
```

Requires Python 3.12+, Docker, and a running Postgres instance for integration tests.

## Environment Variables

| Variable | Service | Description |
|----------|---------|-------------|
| `DATABASE_URL` | linchpin-api | Postgres connection string |
| `LINCHPIN_API_KEY` | both | Bearer token for API auth |
| `CONNECTOR_URL` | linchpin-api | URL of linchpin-connector |
| `ANTHROPIC_API_KEY` | linchpin-api | Anthropic API key |
| `OPENAI_API_KEY` | linchpin-api | OpenAI API key |

## License

MIT
