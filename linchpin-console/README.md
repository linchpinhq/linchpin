# linchpin-console

React + Vite web UI for Linchpin. Browse agents, environments, and sessions; chat with agents live over SSE; approve `always_ask` tool calls inline.

For user-facing context and the API the console talks to, see the root [README.md](../README.md) and [ARCHITECTURE.md](../ARCHITECTURE.md).

## What's inside

| Path | Purpose |
|---|---|
| `src/` | React components, hooks, API client, SSE wiring |
| `index.html` · `vite.config.ts` | Vite entrypoint + build config |
| `tsconfig.json` · `tsconfig.node.json` | TypeScript config |
| `nginx.conf` | Static-serve config used by the production Docker image |
| `Dockerfile` | Multi-stage build: pnpm build → nginx |

## Stack

React · Vite · TypeScript · pnpm. Industrial / Blueprint design system.

## Local development

```bash
pnpm install
pnpm dev          # http://localhost:5173 by default
```

The console calls the API at `VITE_API_URL` (build-time injected, defaults to `http://localhost:8000`). The bearer token is entered via the in-app `ApiKeyInput` component and persisted in `localStorage` — every request is sent with `Authorization: Bearer <token>`.

## Build

```bash
pnpm build        # outputs to dist/
```

The repo-root `docker-compose.yml` builds the production image (Vite build → nginx) and serves the console at `http://localhost:3000`.
