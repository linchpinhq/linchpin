# linchpin-connector

Internal HTTP service that runs MCP servers (over stdio) and invokes custom HTTP tools on behalf of `linchpin-api`. Lives entirely inside the compose network — no external auth, no external port.

For the cross-service picture, see the root [ARCHITECTURE.md](../ARCHITECTURE.md).

## What's inside

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app exposing `POST /tools/invoke` |
| `app/mcp.py` | MCP server lifecycle: one stdio subprocess per (session, server) |
| `app/http_tools.py` | Custom HTTP tool dispatch |
| `tests/` | Pytest suite |

## Wire contract

`linchpin-api` POSTs to `/tools/invoke` with a `ToolInvokeRequest`:

```python
class ToolInvokeRequest(BaseModel):
    session_id: str
    tool_type: Literal["mcp", "custom_http"]
    server_name: str | None       # for MCP
    tool_name: str
    arguments: dict
    credentials: dict | None      # env vars resolved from vault, for MCP
    endpoint: str | None          # for custom HTTP
```

For MCP calls, the connector spawns (or reuses) a stdio subprocess for `(session_id, server_name)` with `credentials` injected as env vars. For custom HTTP calls, it POSTs `arguments` to `endpoint` and returns the response.

## Stack

Python 3.12 · FastAPI · httpx · pytest.

## Local development

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8001
pytest
```

## Security note

The connector is **not** authenticated. It must never be exposed outside the compose network — `docker-compose.yml` keeps its port internal-only.
