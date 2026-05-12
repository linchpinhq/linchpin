# Linchpin

> **Status:** Archived. No further development. Superseded by a clean-room rewrite.

Linchpin is an open standard and self-hostable runtime for **managed AI agents**. It lets you run a managed-agent system on your own infrastructure, against any model provider, without vendor lock-in.

The MVP delivers five core capabilities:

1. **Agents** — versioned configurations (model, system prompt, tools, MCP servers, per-tool permissions)
2. **Environments** — container templates with configurable networking
3. **Sessions** — agent runs inside per-session Docker containers
4. **Event streaming** — SSE with cursor-based replay and pagination
5. **Tool execution** — built-in tools, MCP servers, and custom HTTP tools, all gated by policy

Target deployment: a single VM via `docker-compose`.

## Architecture

```mermaid
graph TB
    Client[Client / SDK / Console]

    subgraph Compose["docker-compose (single VM)"]
        API[linchpin-api<br/>FastAPI · Python 3.12]
        Connector[linchpin-connector<br/>Python 3.12]
        Console[linchpin-console<br/>React + Vite]
        PG[(Postgres 16)]

        subgraph Sandboxes["Per-session containers"]
            C1[Container 1]
            C2[Container 2]
        end
    end

    LLM[LLM Provider<br/>Anthropic · OpenAI · Ollama]
    MCP[MCP servers<br/>stdio subprocesses]
    Ext[Custom HTTP tools]

    Client -->|HTTP + SSE| API
    Console -->|HTTP + SSE| API
    API -->|SQL · LISTEN/NOTIFY| PG
    API -->|POST /tools/invoke| Connector
    API -->|docker-py| C1
    API -->|docker-py| C2
    API -->|HTTP| LLM
    Connector -->|stdio| MCP
    Connector -->|HTTP| Ext
```

### Components

| Service | Stack | Purpose |
|---|---|---|
| `linchpin-api` | FastAPI · asyncpg · docker-py | HTTP surface, orchestrator loop, sandbox manager, built-in tools, SSE streaming, credential vaults |
| `linchpin-connector` | Python · httpx | MCP server lifecycle (stdio transport), custom HTTP tool invocation |
| `linchpin-console` | React · Vite · TypeScript | Web UI served via nginx |
| `postgres` | Postgres 16 | Persistent state, event log, `LISTEN/NOTIFY` fanout to orchestrators |

### Session state machine

```mermaid
stateDiagram-v2
    [*] --> running: session created
    running --> idle: model turn complete
    idle --> running: user message
    running --> running: tool call
    idle --> terminated: user terminates
    running --> terminated: user terminates
    running --> failed: unrecoverable error
    [*] --> rescheduling: process restart
    rescheduling --> idle: recovery complete
```

### Orchestrator loop

One async task per live session. It:

1. Builds conversation context from the event log
2. Calls the model provider
3. Emits `agent.message` / `agent.thinking` / `agent.tool_use` events
4. Evaluates the policy for each tool call:
   - `always_allow` → execute → emit `agent.tool_result` → loop
   - `always_ask` → emit `session.requires_action`, block on `LISTEN/NOTIFY` until a `user.tool_confirmation` arrives
5. On `user.interrupt`, halts the turn and transitions to `idle`
6. On unrecoverable error, emits `session.error` and transitions to `failed`
7. On process restart, replays the event log for non-terminal sessions (`rescheduling` → `idle`)

## Quick start

```bash
# 1. Configure
cp .env.example .env   # then edit LINCHPIN_API_KEY, ANTHROPIC_API_KEY / OPENAI_API_KEY, VAULT_ENCRYPTION_KEY

# 2. Bring up the stack
docker compose up --build
```

Services:

| URL | What |
|---|---|
| `http://localhost:8000` | linchpin-api |
| `http://localhost:8001` | linchpin-connector (internal — no auth) |
| `http://localhost:3000` | linchpin-console |
| `localhost:5432` | Postgres |

### Required environment variables

| Variable | Notes |
|---|---|
| `LINCHPIN_API_KEY` | Bearer token for client → api auth |
| `DATABASE_URL` | Set by compose; `postgresql://linchpin:linchpin@postgres:5432/linchpin` |
| `CONNECTOR_URL` | Set by compose; `http://linchpin-connector:8001` |
| `CORS_ALLOWED_ORIGINS` | Comma-separated origins for the API |
| `ANTHROPIC_API_KEY` | Optional — required to use the Anthropic provider |
| `OPENAI_API_KEY` | Optional — required to use the OpenAI provider |
| `VAULT_ENCRYPTION_KEY` | Fernet key for the credential vault (32 url-safe base64 bytes) |
| `VITE_API_URL` | Build-time API URL for the console |

## HTTP API

All endpoints are versioned under `/v1` and require `Authorization: Bearer $LINCHPIN_API_KEY`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/agents` | Create agent |
| `GET` | `/v1/agents` · `/v1/agents/{id}` | List / fetch agents |
| `POST` | `/v1/environments` | Create environment |
| `GET` | `/v1/environments` · `/v1/environments/{id}` | List / fetch environments |
| `POST` | `/v1/sessions` | Create session (provisions a container, starts the orchestrator) |
| `GET` | `/v1/sessions` · `/v1/sessions/{id}` | List / fetch sessions |
| `DELETE` | `/v1/sessions/{id}` | Terminate session |
| `POST` | `/v1/sessions/{id}/archive` | Archive session |
| `POST` | `/v1/sessions/{id}/events` | Append a `user.*` event |
| `GET` | `/v1/sessions/{id}/events` | Cursor-paginated event history |
| `GET` | `/v1/sessions/{id}/stream` | SSE stream (supports `?cursor=` replay) |
| `POST` | `/v1/vaults` · `/v1/vaults/{id}/credentials` | Credential vaults |

### Event taxonomy

- **User:** `user.message`, `user.interrupt`, `user.tool_confirmation`, `user.custom_tool_result`
- **Agent:** `agent.message`, `agent.thinking`, `agent.tool_use`, `agent.tool_result`, `agent.mcp_tool_use`, `agent.mcp_tool_result`, `agent.custom_tool_use`
- **Session:** `session.status_running`, `session.status_idle`, `session.status_rescheduled`, `session.status_terminated`, `session.error`, `session.requires_action`

## Tools

### Built-in (executed inside the session container by `linchpin-api`)

`bash`, `read`, `write`, `edit`, `glob`, `grep`, `web_fetch`, `web_search`

### MCP servers

Configured per agent with `command`, `args`, `env`. The connector spawns each as a stdio subprocess for the session's lifetime.

### Custom HTTP tools

Configured per agent with an `endpoint`. The connector forwards calls and returns the response payload to the orchestrator.

### Permissions

Each tool entry on an agent has a `permission`:

- `always_allow` — execute immediately
- `always_ask` — emit `session.requires_action`, wait for `user.tool_confirmation`

## Sandboxing

Each session gets its own container built from a base image (Ubuntu 22.04 · Python 3.12 · Node 20 · git · curl · jq · ripgrep). Networking is controlled by the Environment:

| `networking.type` | Docker network |
|---|---|
| `none` | `linchpin-none` (no egress) |
| `unrestricted` | `linchpin-open` |

The API container needs the host Docker socket mounted (`/var/run/docker.sock`).

## Credential vaults

Encrypted (Fernet, AES-128-CBC + HMAC) credential storage scoped to a vault. Credentials are referenced by name from agent MCP server configs and resolved at session start. See [`linchpin-api/app/encryption.py`](linchpin-api/app/encryption.py) and the [`credential-vaults` spec](.kiro/specs/credential-vaults/).

## Repository layout

```
linchpin/
├── linchpin-api/          # FastAPI service
│   ├── app/
│   │   ├── main.py        # FastAPI app + lifespan
│   │   ├── routes/        # agents, environments, sessions, vaults
│   │   ├── orchestrator.py
│   │   ├── sandbox.py     # Docker sandbox protocol
│   │   ├── providers.py   # Anthropic / OpenAI / Ollama adapters
│   │   ├── tools.py       # built-in tools
│   │   ├── policy.py      # permission evaluator
│   │   ├── streaming.py   # SSE
│   │   ├── events.py      # event log + LISTEN/NOTIFY
│   │   ├── credentials.py · encryption.py
│   │   ├── db.py · models.py · migrations.py · auth.py
│   ├── alembic/           # schema migrations
│   └── tests/
├── linchpin-connector/    # MCP + custom HTTP tool runner
│   └── app/{main.py, mcp.py, http_tools.py}
├── linchpin-console/      # React + Vite UI
├── .kiro/specs/           # design / requirements / tasks docs per feature
└── docker-compose.yml
```

## Development

```bash
# API
cd linchpin-api
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload

# Tests
pytest

# Connector
cd ../linchpin-connector
uv pip install -e ".[dev]"
pytest
```

Specs for each feature live under `.kiro/specs/` (requirements · design · tasks).

## License

Not specified.
