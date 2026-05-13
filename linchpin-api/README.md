# linchpin-api

FastAPI service that exposes the Linchpin HTTP + SSE surface, runs the per-session orchestrator loop, drives the Docker sandboxes, calls model providers, and persists state in Postgres.

For the cross-service picture (topology, session state machine, event taxonomy), see the root [ARCHITECTURE.md](../ARCHITECTURE.md). For user-facing docs, see the root [README.md](../README.md).

## What's inside

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app + lifespan (pre-creates Docker networks, recovers in-flight sessions) |
| `app/routes/` | `/v1` endpoints — agents, environments, sessions, events, vaults |
| `app/orchestrator.py` | Per-session async loop: context → model → events → tool dispatch |
| `app/providers.py` | Model provider adapters: `openrouter`, `ollama` (both raw `httpx`) |
| `app/sandbox.py` | Docker sandbox protocol (`docker-py` against the host socket) |
| `app/tools.py` | Built-in tools: `bash`, `read`, `write`, `edit`, `glob`, `grep`, `web_fetch`, `web_search` |
| `app/policy.py` | Per-tool permission evaluator (`always_allow` / `always_ask`) |
| `app/streaming.py` | SSE stream with cursor replay |
| `app/events.py` | Append-only event log + Postgres `LISTEN/NOTIFY` fanout |
| `app/credentials.py` · `app/encryption.py` | Credential vaults (Fernet) |
| `app/db.py` · `app/models.py` · `app/auth.py` | asyncpg pool, ORM, bearer auth |
| `alembic/` | Schema migrations |
| `tests/` | Pytest suite (unit + integration) |

## Stack

Python 3.12 · FastAPI · asyncpg · docker-py · httpx · Pydantic · Alembic · pytest.

## Local development

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
pytest
```

Requires a running Postgres 16 (the repo's `docker-compose.yml` brings one up) and `DATABASE_URL` pointed at it.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `LINCHPIN_API_KEY` | yes | Bearer token clients send as `Authorization: Bearer …` |
| `VAULT_ENCRYPTION_KEY` | yes | Fernet key (32 url-safe base64 bytes) for credential encryption |
| `DATABASE_URL` | yes | asyncpg DSN |
| `OPENROUTER_API_KEY` | for cloud models | Used by the `openrouter` provider adapter. Get one at <https://openrouter.ai/keys> |
| `CORS_ALLOWED_ORIGINS` | no | Defaults to `http://localhost:3000` |

## Docker

The service is containerized for the repo-root `docker-compose.yml`. The api container mounts `/var/run/docker.sock` so it can create per-session sandbox containers on the host Docker daemon.
