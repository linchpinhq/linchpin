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
from app.routes.sessions import router as sessions_router
from app.routes.vaults import router as vaults_router
from app.sandbox import DockerSandbox, ensure_docker_networks

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

    # Start background TTL cleanup task
    ttl_task = asyncio.create_task(
        cleanup_expired_sessions(app.state.sandbox, app.state.orchestrator_tasks)
    )
    app.state.ttl_cleanup_task = ttl_task

    # Recover non-terminal sessions
    await recover_sessions(app.state.sandbox, app.state.orchestrator_tasks)

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

    # Close database connection pool
    await close_pool()

    logger.info("linchpin-api shut down.")


app = FastAPI(
    title="Linchpin API",
    version="0.1.0",
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
    allow_headers=["Authorization"],
)


# Router for all /v1/ endpoints — auth required
v1_router = APIRouter(prefix="/v1", dependencies=[Depends(verify_bearer_token)])

v1_router.include_router(agents_router)
v1_router.include_router(environments_router)
v1_router.include_router(files_router)
v1_router.include_router(sessions_router)
v1_router.include_router(vaults_router)

app.include_router(v1_router)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}
