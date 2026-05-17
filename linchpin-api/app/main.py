"""Linchpin API — FastAPI service for managed AI agents."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Configure root logger so orchestrator and other module logs are visible
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from app.auth import verify_bearer_token
from app.db import close_pool, create_pool
from app.migrations import check_migrations_current
from app.orchestrator import cleanup_expired_sessions, recover_sessions
from app.routes.agents import router as agents_router
from app.routes.environments import router as environments_router
from app.routes.files import router as files_router
from app.routes.memories import router as memories_router
from app.routes.memory_stores import router as memory_stores_router
from app.routes.memory_versions import router as memory_versions_router
from app.routes.session_resources import router as session_resources_router
from app.routes.sessions import router as sessions_router
from app.routes.skills import router as skills_router
from app.routes.vaults import router as vaults_router
from app.routes.webhooks import router as webhooks_router
from app.sandbox import DockerSandbox, ensure_docker_networks
from app.watcher import watch_session_deliverables
from app.webhooks import delivery_worker_loop, worker_enabled

logger = logging.getLogger("linchpin-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler.

    Startup hooks:
    - Run Alembic migration check
    - Create asyncpg database connection pool
    - Pre-create Docker networks (linchpin-none, linchpin-open)
    - Recover non-terminal sessions

    Shutdown hooks:
    - Close database connection pool
    """
    logger.info("Starting linchpin-api...")

    # Verify database migrations are up to date
    check_migrations_current()

    # Create asyncpg database connection pool
    await create_pool()

    # Pre-create Docker networks (linchpin-none, linchpin-open)
    await ensure_docker_networks()

    # Store sandbox instance on app.state for route access
    app.state.sandbox = DockerSandbox()

    # Track orchestrator async tasks per session so they can be cancelled
    app.state.orchestrator_tasks: dict[str, "asyncio.Task"] = {}

    # Track deliverables-watcher tasks per session (PR5 D3). The dict is
    # initialized here (not lazily in create_session) so recover_sessions
    # can populate it for sessions that survived a process restart — that
    # is the wiring that makes the D4 "boot scan on startup" claim true.
    app.state.watcher_tasks: dict[str, "asyncio.Task"] = {}

    # Start background TTL cleanup task
    ttl_task = asyncio.create_task(
        cleanup_expired_sessions(app.state.sandbox, app.state.orchestrator_tasks)
    )
    app.state.ttl_cleanup_task = ttl_task

    # v0.3 PR5 — memory version GC task. Hourly by default; controlled
    # by LINCHPIN_MEMORY_GC_INTERVAL_SEC. Skipped when LINCHPIN_MEMORY_GC=false
    # for test/maintenance windows that don't want the cron tick at all.
    if os.environ.get("LINCHPIN_MEMORY_GC", "true").lower() != "false":
        from app.memory import cleanup_expired_memory_versions
        app.state.memory_gc_task = asyncio.create_task(
            cleanup_expired_memory_versions()
        )
    else:
        app.state.memory_gc_task = None

    # v0.2.0 item #14 — webhook delivery worker. One per process; uses
    # SELECT … FOR UPDATE SKIP LOCKED so multi-process deployments are
    # safe out of the box. ``LINCHPIN_WEBHOOKS_WORKER=false`` skips it
    # entirely (used by tests so the worker doesn't fire HTTP calls).
    if worker_enabled():
        app.state.webhook_worker_task = asyncio.create_task(delivery_worker_loop())
    else:
        app.state.webhook_worker_task = None

    # Recover non-terminal sessions. PR5 — pass watcher_tasks + the
    # watcher spawner so any session with a surviving container gets its
    # deliverables watcher (and the boot scan inside it) re-attached.
    await recover_sessions(
        app.state.sandbox,
        app.state.orchestrator_tasks,
        watcher_tasks=app.state.watcher_tasks,
        spawn_watcher=watch_session_deliverables,
    )

    # Validate VAULT_ENCRYPTION_KEY early (warn, don't crash — key only
    # required when vault features are actually used)
    try:
        from app.encryption import get_encryption_service
        get_encryption_service()
        logger.info("VAULT_ENCRYPTION_KEY validated successfully.")
    except RuntimeError as exc:
        logger.warning("VAULT_ENCRYPTION_KEY not configured: %s — vault features will be unavailable", exc)

    logger.info("linchpin-api started.")
    yield

    # Shutdown
    logger.info("Shutting down linchpin-api...")

    # Cancel TTL cleanup task
    ttl_task.cancel()
    try:
        await ttl_task
    except asyncio.CancelledError:
        pass

    # Cancel webhook delivery worker if running
    wht = getattr(app.state, "webhook_worker_task", None)
    if wht is not None:
        wht.cancel()
        try:
            await wht
        except (asyncio.CancelledError, Exception):
            pass

    # v0.3 PR5 — cancel memory GC task if running.
    mgt = getattr(app.state, "memory_gc_task", None)
    if mgt is not None:
        mgt.cancel()
        try:
            await mgt
        except (asyncio.CancelledError, Exception):
            pass

    # Cancel any in-flight deliverables-watcher tasks (PR5). Boot scan on
    # the next start re-ingests anything written between now and restart.
    for sid, w in list(app.state.watcher_tasks.items()):
        if not w.done():
            w.cancel()
    for sid, w in list(app.state.watcher_tasks.items()):
        try:
            await w
        except (asyncio.CancelledError, Exception):
            pass
    app.state.watcher_tasks.clear()

    # Close database connection pool
    await close_pool()

    logger.info("linchpin-api shut down.")


app = FastAPI(
    title="Linchpin API",
    version="0.6.0",
    description="Self-hostable runtime for managed AI agents",
    lifespan=lifespan,
)

# CORS middleware — allow the console origin (and any custom origins)
cors_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Linchpin-API-Version"],
    expose_headers=["Linchpin-API-Version", "Linchpin-Deprecation"],
)


# v0.2.0 breaking bundle — middleware that resolves the
# `Linchpin-API-Version` header on every request, caches it on
# request.state, and echoes the resolved version back on the response.
# An unknown version short-circuits to 400 with the supported list,
# matching the behavior negotiate() would produce from a dependency.
@app.middleware("http")
async def linchpin_api_version_middleware(request, call_next):  # type: ignore[no-untyped-def]
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse
    from app import api_version as _av
    try:
        version = _av.negotiate(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    response = await call_next(request)
    _av.echo_version_header(response, version)
    return response


# Router for all /v1/ endpoints — auth required
v1_router = APIRouter(prefix="/v1", dependencies=[Depends(verify_bearer_token)])

v1_router.include_router(agents_router)
v1_router.include_router(environments_router)
v1_router.include_router(files_router)
v1_router.include_router(memory_stores_router)
v1_router.include_router(memories_router)
v1_router.include_router(memory_versions_router)
v1_router.include_router(sessions_router)
v1_router.include_router(session_resources_router)
v1_router.include_router(skills_router)
v1_router.include_router(vaults_router)
v1_router.include_router(webhooks_router)

app.include_router(v1_router)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}
