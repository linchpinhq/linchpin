"""Session CRUD endpoints.

POST   /v1/sessions              — Create session
GET    /v1/sessions              — List sessions (paginated, optional agent_id filter)
GET    /v1/sessions/{id}         — Get session by id with stats and usage
POST   /v1/sessions/{id}         — Update session title/metadata
DELETE /v1/sessions/{id}         — Terminate session (set status=terminated, destroy container)
POST   /v1/sessions/{id}/archive — Archive session (set archived_at)

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.6, 6.5, 19.2, 19.3, 20.1, 20.2, 20.3
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sse_starlette.sse import EventSourceResponse

from app.db import fetch_all, fetch_one, execute, listen, notify
from app.events import append_event, decode_cursor, get_events, release_session_event_lock
from app.files import get_file_store
from app.memory import (
    materialize_memory_store,
    max_stores_per_session,
    session_memory_cache_dir,
)
from app.memory_watcher import watch_memory_store
from app.orchestrator import run_session
from app.sandbox import DEFAULT_BASE_IMAGE, ResourceMount, SandboxError
from app.streaming import get_stream
from app.watcher import ensure_session_outputs_dir, session_outputs_dir, watch_session_deliverables
from app.models import (
    CreateSessionRequest,
    EnvironmentPackages,
    EventResponse,
    FileResource,
    MemoryStoreResource,
    PaginatedEventsResponse,
    PaginatedListResponse,
    PostEventsRequest,
    SessionResource,
    SessionResponse,
    SessionStats,
    SessionUsage,
    VaultResource,
)

logger = logging.getLogger("linchpin-api.sessions")

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _row_to_resource(row) -> SessionResource:
    """Convert a session_resources asyncpg Record to a SessionResource."""
    config_raw = row["config"]
    config = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
    return SessionResource(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        type=row["type"],
        mount_path=row["mount_path"],
        config=config,
        state=row["state"],
        error=row["error"],
        created_at=row["created_at"],
        unmounted_at=row["unmounted_at"],
    )


def _row_to_session(row, resources: list[SessionResource] | None = None) -> SessionResponse:
    """Convert an asyncpg Record to a SessionResponse."""
    stats_raw = row["stats"]
    usage_raw = row["usage"]
    metadata_raw = row["metadata"]

    stats = json.loads(stats_raw) if isinstance(stats_raw, str) else stats_raw
    usage = json.loads(usage_raw) if isinstance(usage_raw, str) else usage_raw
    metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else metadata_raw

    # Parse vault_ids — may be a JSON string, list, or missing
    vault_ids_raw = row.get("vault_ids", [])
    if vault_ids_raw is None:
        vault_ids = []
    elif isinstance(vault_ids_raw, str):
        vault_ids = json.loads(vault_ids_raw)
    else:
        vault_ids = vault_ids_raw

    return SessionResponse(
        id=str(row["id"]),
        agent_id=str(row["agent_id"]),
        agent_version=row["agent_version"],
        environment_id=str(row["environment_id"]),
        status=row["status"],
        container_id=row["container_id"],
        title=row["title"],
        metadata=metadata,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
        last_event_cursor=row["last_event_cursor"],
        ttl_seconds=row["ttl_seconds"],
        stats=SessionStats(**stats),
        usage=SessionUsage(**usage),
        vault_ids=vault_ids,
        resources=resources or [],
    )


async def _load_session_resources(session_id: uuid.UUID) -> list[SessionResource]:
    """Load all session_resources rows for a session (any state)."""
    rows = await fetch_all(
        """SELECT * FROM session_resources
           WHERE session_id = $1
           ORDER BY created_at ASC""",
        session_id,
    )
    return [_row_to_resource(r) for r in rows]


def _network_for_environment(config: dict) -> str:
    """Determine the Docker network name from an environment config dict.

    v0.2.0 item #4 — `limited` mode maps to `linchpin-limited`. In v0.2.0
    that network is an internal bridge (same as `linchpin-none`); v0.2.x
    will add the egress proxy that turns the env's `allowed_hosts` /
    `allow_mcp_servers` / `allow_package_managers` lists into actual
    permitted traffic.
    """
    networking = config.get("networking", {})
    net_type = networking.get("type", "none")
    if net_type == "unrestricted":
        return "linchpin-open"
    if net_type == "limited":
        return "linchpin-limited"
    return "linchpin-none"


@router.post("", status_code=201, response_model=SessionResponse)
async def create_session(
    body: CreateSessionRequest, request: Request, response: Response
) -> SessionResponse:
    """Create a new session: validate refs, provision container, insert record."""
    # Validate agent_id exists
    try:
        agent_uid = uuid.UUID(body.agent_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Agent {body.agent_id} not found"},
        )

    agent_row = await fetch_one("SELECT * FROM agents WHERE id = $1", agent_uid)
    if agent_row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Agent {body.agent_id} not found"},
        )

    # Validate environment_id exists
    try:
        env_uid = uuid.UUID(body.environment_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Environment {body.environment_id} not found"},
        )

    env_row = await fetch_one("SELECT * FROM environments WHERE id = $1", env_uid)
    if env_row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Environment {body.environment_id} not found"},
        )

    # v0.3 PR6 — vault_ids → resources[] fold-in. Accept both shapes:
    # legacy ``vault_ids: [...]`` (deprecated) and ``resources: [{type:
    # vault, vault_id: ...}]`` (new). Normalize to a single deduped list
    # for validation and persistence. The original ``body.vault_ids``
    # flag drives the deprecation signaling below; the union drives the
    # vault existence + archive check.
    legacy_vault_ids_supplied = bool(body.vault_ids)
    vault_resources_from_resources = [
        r.vault_id for r in body.resources if isinstance(r, VaultResource)
    ]
    seen: set[str] = set()
    unified_vault_ids: list[str] = []
    for vid in list(body.vault_ids) + vault_resources_from_resources:
        if vid in seen:
            continue
        seen.add(vid)
        unified_vault_ids.append(vid)

    # Validate vault_ids: each must exist and not be archived
    if unified_vault_ids:
        for vid in unified_vault_ids:
            try:
                vault_uid = uuid.UUID(vid)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": f"Invalid vault id: {vid}",
                    },
                )
            vault_row = await fetch_one("SELECT id, archived_at FROM vaults WHERE id = $1", vault_uid)
            if vault_row is None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": f"Vault {vid} not found",
                    },
                )
            if vault_row["archived_at"] is not None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": f"Vault {vid} is archived",
                    },
                )

    # Validate resources[] — v0.3 adds memory_store dispatch. `file` and
    # `memory_store` are accepted; `github_repository` still 501s. File
    # resources must reference an existing, unarchived upload (not a
    # deliverable — re-mounting deliverables across sessions would be a covert
    # channel; explicit re-upload is required).
    #
    # PR3 closes PR2's TOCTOU note for files: the upfront validation block here
    # checks existence + source + storage_path. The mount-time re-check below
    # catches the narrow race between this block and container creation (file
    # deleted between the two) and transitions the resource to state='failed'
    # with a session.resource_mount_failed event — the spec's "session still
    # starts" path.
    #
    # Memory stores are looked up here too; failures (missing/archived store,
    # cap exceeded, duplicate store_id in the same request) fail the whole
    # create — they're not deferrable the way a missing file is, because we
    # need the store row to materialize the host cache and to emit the system
    # prompt block before the agent runs.
    file_meta: list[dict[str, Any] | None] = []   # parallel; None for non-file
    memory_meta: list[dict[str, Any] | None] = [] # parallel; None for non-memory
    seen_memory_store_ids: set[str] = set()
    for resource in body.resources:
        if isinstance(resource, FileResource):
            try:
                file_uid = uuid.UUID(resource.file_id)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": f"Invalid file id: {resource.file_id}",
                    },
                )
            file_row = await fetch_one(
                "SELECT id, source, archived_at, storage_path FROM files WHERE id = $1",
                file_uid,
            )
            if file_row is None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": f"File {resource.file_id} not found",
                    },
                )
            if file_row["archived_at"] is not None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": f"File {resource.file_id} is archived",
                    },
                )
            if file_row["source"] != "upload":
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": (
                            f"File {resource.file_id} has source='{file_row['source']}'; "
                            "only uploads can be mounted as session resources"
                        ),
                    },
                )
            file_meta.append({
                "file_uid": file_uid,
                "storage_path": file_row["storage_path"],
            })
            memory_meta.append(None)
        elif isinstance(resource, MemoryStoreResource):
            try:
                store_uid = uuid.UUID(resource.memory_store_id)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": f"Invalid memory_store id: {resource.memory_store_id}",
                    },
                )
            # Dedupe by canonical UUID form so callers can't bypass the check
            # via dashes/case/braces variations on the same id.
            canonical_id = str(store_uid)
            if canonical_id in seen_memory_store_ids:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": (
                            f"memory_store {canonical_id} appears more than once in "
                            "resources[]; attach each store at most once per session"
                        ),
                    },
                )
            seen_memory_store_ids.add(canonical_id)
            store_row = await fetch_one(
                "SELECT id, name, description, archived_at "
                "FROM memory_stores WHERE id = $1",
                store_uid,
            )
            if store_row is None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": f"Memory store {canonical_id} not found",
                    },
                )
            if store_row["archived_at"] is not None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_error",
                        "message": f"Memory store {canonical_id} is archived",
                    },
                )
            file_meta.append(None)
            memory_meta.append({
                "store_uid": store_uid,
                "name": store_row["name"],
                "description": store_row["description"],
            })
        elif isinstance(resource, VaultResource):
            # v0.3 PR6 — VaultResource entries piggy-back on the
            # ``unified_vault_ids`` validation pass above. Skip per-
            # resource work here so the parallel meta lists stay
            # aligned with body.resources by index.
            file_meta.append(None)
            memory_meta.append(None)
        else:
            raise HTTPException(
                status_code=501,
                detail={
                    "error": "not_implemented",
                    "message": (
                        f"resource type '{resource.type}' is not supported in v0.3; "
                        "only 'file' and 'memory_store' are dispatchable in this release"
                    ),
                },
            )

    # Per-session memory-store cap — mirrors Anthropic's max-8 default
    # (overridable via LINCHPIN_MEMORY_MAX_STORES_PER_SESSION). Enforce after
    # the validation loop so the error message reports the actual count.
    memory_resource_count = sum(1 for m in memory_meta if m is not None)
    cap = max_stores_per_session()
    if memory_resource_count > cap:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "validation_error",
                "message": (
                    f"session has {memory_resource_count} memory_store resources; "
                    f"cap is {cap} per session"
                ),
            },
        )

    # Determine network from environment config
    env_config = env_row["config"]
    if isinstance(env_config, str):
        env_config = json.loads(env_config)
    network = _network_for_environment(env_config)

    # Generate the session id up front so PR5's per-session outputs bind can
    # be set up before the container starts. The id is also used to scope the
    # deliverables watcher task.
    session_id = str(uuid.uuid4())

    # Build the read-only mount list for file resources (D1 — plain `:ro`
    # bind, no overlay2). Per-resource mount-time re-check catches files
    # deleted between upfront validation and now; failures get state='failed'
    # rows and a session.resource_mount_failed event after the session row
    # exists. FileStore is lazy-initialized only when there are resources to
    # mount so v0.1-shape session creates (no resources) don't trigger a
    # mkdir on LINCHPIN_FILES_ROOT.
    #
    # Memory stores skip this loop entirely — PR3 only registers them in
    # session_resources and materializes the host cache (below). PR4 turns
    # that cache into an actual sandbox bind + adds the per-store watcher.
    mounts: list[ResourceMount] = []
    resource_states: list[tuple[str, str | None]] = []  # parallel to body.resources
    seen_host_paths: dict[str, str] = {}  # host_path -> mount_path of first claimant
    has_file_resource = any(isinstance(r, FileResource) for r in body.resources)
    file_store = get_file_store() if has_file_resource else None
    for idx, resource in enumerate(body.resources):
        if not isinstance(resource, FileResource):
            # memory_store rows are recorded after the session row exists;
            # github_repository is rejected upstream. Anything else here is
            # a bug, not a runtime input.
            resource_states.append(("mounted", None))
            continue
        if file_store is None:
            raise RuntimeError("file_store unexpectedly None despite file resource present")
        meta = file_meta[idx]
        if meta is None:
            raise RuntimeError(
                f"file_meta[{idx}] is None but resource is FileResource — "
                "validation/mount-build loops are out of step"
            )
        host_path = file_store.absolute_path(meta["storage_path"])
        # Refuse two resources that resolve to the same backing host_path —
        # the FileStore is content-addressed so two distinct file_ids whose
        # bytes match share storage_path, and docker-py's ``volumes=`` shape
        # is host-path-keyed (it silently collapses dupes into the last one).
        # Returning 422 here means the API never reports state='mounted' for
        # a bind that didn't actually reach docker. Lifting this to "mount
        # same content at multiple paths" needs the docker-py ``mounts=`` API.
        if host_path in seen_host_paths:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "duplicate_mount_source",
                    "message": (
                        f"resources[{idx}] (file_id={resource.file_id}, "
                        f"mount_path={resource.mount_path!r}) backs onto the "
                        "same content-addressed storage as an earlier resource "
                        f"already mounted at {seen_host_paths[host_path]!r}. "
                        "Mounting one file at multiple container paths is not "
                        "supported in v0.2.0; upload the file as a distinct "
                        "object or pick one mount path."
                    ),
                },
            )
        if not os.path.isfile(host_path):
            resource_states.append((
                "failed",
                (
                    f"file {resource.file_id} backing path is missing on disk "
                    f"(storage_path={meta['storage_path']!r})"
                ),
            ))
            continue
        seen_host_paths[host_path] = resource.mount_path
        mounts.append(ResourceMount(
            host_path=host_path,
            container_path=resource.mount_path,
            mode="ro",
        ))
        resource_states.append(("mounted", None))

    # PR5 — writable bind for deliverables. The container sees
    # /mnt/session/outputs/ as writable; the watcher (asyncio task per
    # session, spawned below) mirrors host-side writes into the Files API.
    outputs_dir = ensure_session_outputs_dir(session_id)
    mounts.append(ResourceMount(
        host_path=str(outputs_dir),
        container_path="/mnt/session/outputs",
        mode="rw",
    ))

    # v0.3 PR3 + PR4 — materialize each attached memory_store into its
    # per-session host cache dir and add a bind mount so the container
    # sees it at /mnt/memory/<name>/. PR4 also spawns a watcher per
    # rw store after the session row exists (below). We materialize +
    # mount before container create so any hydration failure (disk full,
    # bad storage row) 500s cleanly with no orphan container or DB state
    # to roll back.
    for idx, resource in enumerate(body.resources):
        if not isinstance(resource, MemoryStoreResource):
            continue
        meta = memory_meta[idx]
        if meta is None:
            raise RuntimeError(
                f"memory_meta[{idx}] is None but resource is MemoryStoreResource — "
                "validation/materialize loops are out of step"
            )
        try:
            cache_dir = await materialize_memory_store(
                session_id=session_id,
                memory_store_id=meta["store_uid"],
                store_name=meta["name"],
            )
        except OSError as exc:
            logger.error(
                "materialize_memory_store failed (session=%s, store=%s): %s",
                session_id, meta["store_uid"], exc,
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "memory_materialize_failed",
                    "message": (
                        f"failed to hydrate memory store {meta['store_uid']} "
                        f"into per-session cache: {exc}"
                    ),
                },
            ) from exc
        # Bind mount mode mirrors the resource's access field. Read-only
        # stores get a kernel-enforced ro bind, so the agent can't write
        # at all and PR4's watcher doesn't run for them. Read-write stores
        # get rw + a writeback watcher (spawned after session row insert).
        mounts.append(ResourceMount(
            host_path=cache_dir,
            container_path=f"/mnt/memory/{meta['name']}",
            mode="rw" if resource.access == "read_write" else "ro",
        ))

    # Provision Docker container via sandbox with the mount list.
    sandbox = request.app.state.sandbox

    # v0.2.0 item #3 — derive a packaged image when the environment lists
    # any pre-install packages. Empty package set falls through to the
    # base image at no extra cost.
    base_image = os.getenv("LINCHPIN_SANDBOX_IMAGE", DEFAULT_BASE_IMAGE)
    packages_dict = env_config.get("packages") if isinstance(env_config, dict) else None
    env_packages = EnvironmentPackages.model_validate(packages_dict or {})
    try:
        image = await sandbox.ensure_image(
            base_image=base_image, packages=env_packages
        )
    except SandboxError as exc:
        logger.error("sandbox.ensure_image failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "sandbox_image_build_failed",
                "message": str(exc),
            },
        ) from exc

    try:
        container_id = await sandbox.create(image, network, mounts=mounts)
    except SandboxError as exc:
        # Container creation is all-or-nothing in docker-py — if it fails,
        # there's no partial session to leave behind. Raise 500 with detail.
        logger.error("sandbox.create failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "sandbox_create_failed",
                "message": str(exc),
            },
        ) from exc

    # Everything below this point depends on the container we just created.
    # If any DB write fails (CHECK constraint, transient connection drop,
    # cancellation), we must destroy the container or it'll run forever with
    # no row pointing at it — recover_sessions only walks the sessions
    # table. Wrap the whole block and clean up on any failure.
    # (session_id was generated above so the PR5 outputs bind could use it.)
    try:
        agent_version = agent_row["version"]
        metadata_json = json.dumps(body.metadata)
        # v0.3 PR6 — persist the unified list (legacy vault_ids + any
        # VaultResource entries pulled from resources[]). Storage column
        # is unchanged; the fold-in only normalizes the input shape.
        vault_ids_json = json.dumps(unified_vault_ids)
        stats_json = json.dumps({"total_events": 0, "tool_calls": 0, "model_turns": 0})
        # v0.2.0 item #10 — prompt-cache counters seeded alongside the existing two.
        usage_json = json.dumps({
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        })

        # v0.2.0 breaking bundle — pin the session to the wire-format
        # version the caller used. NULL = v1 (no header sent). The events
        # endpoint reads this column to keep an in-flight stream's shape
        # consistent for the session's lifetime.
        from app.api_version import V1
        api_version = getattr(request.state, "api_version", V1)
        pinned_version: str | None = None if api_version == V1 else api_version

        row = await fetch_one(
            """
            INSERT INTO sessions
                (id, agent_id, agent_version, environment_id, status,
                 container_id, title, metadata, ttl_seconds, vault_ids, stats, usage,
                 linchpin_api_version)
            VALUES ($1, $2, $3, $4, 'running',
                    $5, $6, $7::jsonb, $8, $9::jsonb, $10::jsonb, $11::jsonb, $12)
            RETURNING *
            """,
            uuid.UUID(session_id),
            agent_uid,
            agent_version,
            env_uid,
            container_id,
            body.title,
            metadata_json,
            body.ttl_seconds,
            vault_ids_json,
            stats_json,
            usage_json,
            pinned_version,
        )

        # Persist session_resources rows. State is 'mounted' for resources whose
        # bind was added to the container (file) or whose host cache was
        # materialized (memory_store); 'failed' (with error) for file resources
        # whose host_path went missing between validation and mount.
        resources: list[SessionResource] = []
        failed_resources: list[tuple[uuid.UUID, str, str]] = []  # (resource_id, mount_path, error)
        for idx, resource in enumerate(body.resources):
            if isinstance(resource, FileResource):
                state, error = resource_states[idx]
                # Persist canonical UUID form (str(uuid.UUID(x))) so the DELETE 409
                # mount-conflict check (which uses str(file_uid) on the URL-supplied
                # id) matches regardless of whether the original POST sent the file
                # id with dashes, without dashes, in uppercase, or braced.
                resource_type = resource.type
                resource_mount_path = resource.mount_path
                config_json = json.dumps({"file_id": str(uuid.UUID(resource.file_id))})
            elif isinstance(resource, MemoryStoreResource):
                meta = memory_meta[idx]
                if meta is None:
                    raise RuntimeError(
                        f"memory_meta[{idx}] is None at persist time — "
                        "validation/persist loops are out of step"
                    )
                state, error = "mounted", None
                resource_type = resource.type
                # /mnt/memory/<store-name>/ is the canonical container-side
                # path PR4 will bind to. We persist it now so the orchestrator
                # block + REST responses can reference the path without
                # round-tripping memory_stores.
                resource_mount_path = f"/mnt/memory/{meta['name']}"
                config_json = json.dumps({
                    "memory_store_id": str(meta["store_uid"]),
                    "access": resource.access,
                    "instructions": resource.instructions,
                })
            elif isinstance(resource, VaultResource):
                # v0.3 PR6 — VaultResource doesn't get a session_resources
                # row; the canonical surface is still sessions.vault_ids.
                # The fold-in is purely an input-shape convenience.
                continue
            else:
                raise RuntimeError(
                    f"unexpected resource type {type(resource).__name__} reached persist loop"
                )
            # Terminal states ('failed', 'unmounted') require unmounted_at per
            # the session_resources_terminal_has_unmounted_at CHECK in
            # migration 0004. Set unmounted_at=now() for pre-mount failures.
            unmounted_at = datetime.now(timezone.utc) if state == "failed" else None
            resource_row = await fetch_one(
                """
                INSERT INTO session_resources
                    (session_id, type, mount_path, config, state, error, unmounted_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                RETURNING *
                """,
                uuid.UUID(session_id),
                resource_type,
                resource_mount_path,
                config_json,
                state,
                error,
                unmounted_at,
            )
            resources.append(_row_to_resource(resource_row))
            if state == "failed":
                failed_resources.append((resource_row["id"], resource_mount_path, error or ""))

        # Emit session.resource_mount_failed events for any pre-mount failures.
        # The session row is in place by this point so the events table's FK
        # constraint (session_id REFERENCES sessions) is satisfied. Spec line
        # 336: session still starts even when individual mounts fail.
        for resource_uid, mount_path, error in failed_resources:
            try:
                await append_event(
                    session_id,
                    "session.resource_mount_failed",
                    {
                        "resource_id": str(resource_uid),
                        "mount_path": mount_path,
                        "error": error,
                    },
                )
            except Exception:
                # Best-effort: event log is not load-bearing for session boot.
                logger.exception("failed to append session.resource_mount_failed event")

        # v0.3 PR6 — vault_ids deprecation signal. When the caller sent
        # the legacy ``vault_ids`` field (even an empty list won't trip
        # because legacy_vault_ids_supplied was set off a truthy check),
        # echo the deprecation back so SDKs can surface it to users and
        # emit one session.deprecation_used event so a downstream audit
        # query can see which clients are still on the old shape.
        if legacy_vault_ids_supplied:
            from app.api_version import (
                DEPRECATED_SESSION_VAULT_IDS,
                mark_deprecation,
            )
            mark_deprecation(response, DEPRECATED_SESSION_VAULT_IDS)
            try:
                await append_event(
                    session_id,
                    "session.deprecation_used",
                    {
                        "shape": DEPRECATED_SESSION_VAULT_IDS,
                        "migration": (
                            "Send vaults via resources[] entries with "
                            "type='vault' instead. vault_ids removed in v0.4."
                        ),
                    },
                )
            except Exception:
                logger.exception("failed to append session.deprecation_used event")
    except BaseException as create_exc:
        # Destroy the orphaned container before re-raising so we don't leak
        # docker resources on any DB/cancellation failure post-create. Use
        # BaseException so client-disconnect (CancelledError) also cleans up.
        # Cascade-delete via FK ON DELETE CASCADE removes any partially-
        # written session_resources rows; we still issue an explicit DELETE
        # in case the sessions INSERT itself succeeded and the failure was
        # later (the FK cascade is what's load-bearing for resource rows).
        logger.error(
            "session create failed after container provisioning: %s; "
            "destroying orphan container %s",
            create_exc,
            container_id,
        )
        try:
            await sandbox.destroy(container_id)
        except Exception:
            logger.exception(
                "failed to destroy orphan container %s during session-create rollback",
                container_id,
            )
        try:
            await execute("DELETE FROM sessions WHERE id = $1", uuid.UUID(session_id))
        except Exception:
            logger.exception(
                "failed to delete partial session row %s during rollback", session_id,
            )
        raise

    # Start the orchestrator loop as a background async task
    task = asyncio.create_task(run_session(session_id, sandbox))

    def _task_done(t: asyncio.Task) -> None:
        exc = t.exception() if not t.cancelled() else None
        if exc:
            logger.error("Orchestrator task for session %s failed: %s", session_id, exc)

    task.add_done_callback(_task_done)
    request.app.state.orchestrator_tasks[session_id] = task

    # PR5 — spawn the deliverables watcher as a per-session asyncio task (D3).
    # Watches host-side /mnt/session/outputs/{sid} for new files written by
    # the agent and registers them as deliverables in the Files API. Cancelled
    # in terminate_session.
    watcher_task = asyncio.create_task(watch_session_deliverables(session_id))

    def _watcher_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error("Watcher task for session %s failed: %s", session_id, exc)

    watcher_task.add_done_callback(_watcher_done)
    watcher_tasks = getattr(request.app.state, "watcher_tasks", None)
    if watcher_tasks is None:
        watcher_tasks = {}
        request.app.state.watcher_tasks = watcher_tasks
    watcher_tasks[session_id] = watcher_task

    # v0.3 PR4 — spawn one memory writeback watcher per attached read_write
    # memory_store resource. Read-only stores get no watcher (the kernel
    # rejects writes at the bind mount). Each task is keyed by session id
    # so terminate_session can cancel them as a group.
    memory_watcher_tasks_by_session = getattr(request.app.state, "memory_watcher_tasks", None)
    if memory_watcher_tasks_by_session is None:
        memory_watcher_tasks_by_session = {}
        request.app.state.memory_watcher_tasks = memory_watcher_tasks_by_session
    spawned: list[asyncio.Task] = []
    for idx, resource in enumerate(body.resources):
        if not isinstance(resource, MemoryStoreResource):
            continue
        if resource.access != "read_write":
            continue
        meta = memory_meta[idx]
        if meta is None:
            continue
        mw_task = asyncio.create_task(
            watch_memory_store(
                session_id,
                meta["store_uid"],
                meta["name"],
            )
        )

        def _mw_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(
                    "Memory watcher task for session %s failed: %s", session_id, exc,
                )

        mw_task.add_done_callback(_mw_done)
        spawned.append(mw_task)
    if spawned:
        memory_watcher_tasks_by_session[session_id] = spawned

    return _row_to_session(row, resources=resources)


@router.get("", response_model=PaginatedListResponse)
async def list_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    agent_id: str | None = Query(default=None),
) -> PaginatedListResponse:
    """Return a paginated list of sessions, optionally filtered by agent_id."""
    if agent_id is not None:
        try:
            agent_uid = uuid.UUID(agent_id)
        except ValueError:
            return PaginatedListResponse(data=[], has_more=False, next_cursor=None)

        rows = await fetch_all(
            """SELECT * FROM sessions
               WHERE agent_id = $1
               ORDER BY created_at DESC
               LIMIT $2 OFFSET $3""",
            agent_uid,
            limit + 1,
            offset,
        )
    else:
        rows = await fetch_all(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            limit + 1,
            offset,
        )

    has_more = len(rows) > limit
    items = rows[:limit]

    # Batch-load resources for all returned sessions in one query (avoids N+1).
    session_ids = [r["id"] for r in items]
    resources_by_session: dict[uuid.UUID, list[SessionResource]] = {sid: [] for sid in session_ids}
    if session_ids:
        resource_rows = await fetch_all(
            """SELECT * FROM session_resources
               WHERE session_id = ANY($1)
               ORDER BY created_at ASC""",
            session_ids,
        )
        for r in resource_rows:
            resources_by_session.setdefault(r["session_id"], []).append(_row_to_resource(r))

    sessions = [_row_to_session(r, resources=resources_by_session[r["id"]]) for r in items]

    return PaginatedListResponse(data=sessions, has_more=has_more, next_cursor=None)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str) -> SessionResponse:
    """Retrieve a session by id with stats and usage."""
    try:
        uid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    row = await fetch_one("SELECT * FROM sessions WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )
    resources = await _load_session_resources(uid)
    return _row_to_session(row, resources=resources)


@router.post("/{session_id}", response_model=SessionResponse)
async def update_session(session_id: str, body: dict[str, Any]) -> SessionResponse:
    """Update session title and/or metadata."""
    try:
        uid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    row = await fetch_one("SELECT * FROM sessions WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    # Build SET clauses for provided fields
    sets: list[str] = ["updated_at = now()"]
    args: list[Any] = []
    idx = 1

    if "title" in body:
        sets.append(f"title = ${idx}")
        args.append(body["title"])
        idx += 1

    if "metadata" in body:
        sets.append(f"metadata = ${idx}::jsonb")
        args.append(json.dumps(body["metadata"]))
        idx += 1

    args.append(uid)
    set_clause = ", ".join(sets)

    updated = await fetch_one(
        f"UPDATE sessions SET {set_clause} WHERE id = ${idx} RETURNING *",
        *args,
    )

    resources = await _load_session_resources(uid)
    return _row_to_session(updated, resources=resources)


@router.delete("/{session_id}", response_model=SessionResponse)
async def terminate_session(session_id: str, request: Request) -> SessionResponse:
    """Terminate a session: set status to terminated, destroy container."""
    try:
        uid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    row = await fetch_one("SELECT * FROM sessions WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    # Update status to terminated
    updated = await fetch_one(
        """UPDATE sessions
           SET status = 'terminated', updated_at = now()
           WHERE id = $1
           RETURNING *""",
        uid,
    )

    # Transition all live session_resources rows to 'unmounted'. Destroying
    # the container removes every bind, so the rows are no longer mounted;
    # this UPDATE makes the DELETE /v1/files 409 check (which filters on
    # state IN ('mounted','unmounting')) reflect reality. Without it, files
    # would become permanently undeletable after first session use even
    # though the underlying bind no longer exists.
    await execute(
        """UPDATE session_resources
           SET state = 'unmounted', unmounted_at = now()
           WHERE session_id = $1
             AND state IN ('mounted', 'unmounting')""",
        uid,
    )

    # Cancel the orchestrator task if it's running
    orchestrator_tasks = getattr(request.app.state, "orchestrator_tasks", {})
    task = orchestrator_tasks.pop(session_id, None)
    if task is not None and not task.done():
        task.cancel()

    # PR5 — cancel the deliverables watcher task. Boot scan on next start
    # (D4) picks up anything written between now and a future restart, so
    # the cancel is safe even if there are in-flight deliverables. Await
    # the cancellation so the rmtree below doesn't race a still-running
    # ingest (POSIX unlink-while-open is safe but we want the watcher's
    # logs to finish before we wipe its directory).
    watcher_tasks = getattr(request.app.state, "watcher_tasks", {})
    watcher_task = watcher_tasks.pop(session_id, None)
    if watcher_task is not None and not watcher_task.done():
        watcher_task.cancel()
        try:
            await watcher_task
        except (asyncio.CancelledError, Exception):
            pass

    # v0.3 PR4 — cancel every memory writeback watcher attached to this
    # session. Boot scan reconciles any drift on the next session resume
    # (there isn't one for terminated sessions, but the symmetry with the
    # deliverables watcher's restart story keeps the model coherent).
    memory_watcher_tasks = getattr(request.app.state, "memory_watcher_tasks", {})
    mw_tasks = memory_watcher_tasks.pop(session_id, [])
    for mw_task in mw_tasks:
        if not mw_task.done():
            mw_task.cancel()
    for mw_task in mw_tasks:
        try:
            await mw_task
        except (asyncio.CancelledError, Exception):
            pass

    # PR5 — terminal cleanup of the session's writable bind directory.
    # During the session this directory is the agent's workspace (the
    # source files are deliberately retained even after the watcher mirrors
    # them into the FileStore). On terminate the workspace is dead state
    # and would otherwise leak up to LINCHPIN_DELIVERABLES_PER_SESSION_CAP_BYTES
    # per session permanently.
    await asyncio.to_thread(
        shutil.rmtree, str(session_outputs_dir(session_id)), True,
    )

    # v0.3 PR4 — wipe the per-session memory cache dir. The canonical
    # state is in DB + FileStore (cache is regenerable). Path mirrors
    # the session_memory_cache_dir() layout: parent of any single store
    # dir is the per-session memory root.
    memory_cache_root = os.path.dirname(
        session_memory_cache_dir(session_id, "_placeholder")
    )
    await asyncio.to_thread(shutil.rmtree, memory_cache_root, True)

    # Drop the per-session events.append lock so the dict doesn't grow
    # unboundedly across the api process's lifetime.
    release_session_event_lock(session_id)

    # Destroy the container
    container_id = row["container_id"]
    if container_id:
        sandbox = request.app.state.sandbox
        await sandbox.destroy(container_id)

    resources = await _load_session_resources(uid)
    return _row_to_session(updated, resources=resources)


@router.post("/{session_id}/archive", response_model=SessionResponse)
async def archive_session(session_id: str) -> SessionResponse:
    """Archive a session: set archived_at. Reject if already archived."""
    try:
        uid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    row = await fetch_one("SELECT * FROM sessions WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    if row["archived_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "message": f"Session {session_id} is already archived"},
        )

    updated = await fetch_one(
        """UPDATE sessions
           SET archived_at = now(), updated_at = now()
           WHERE id = $1
           RETURNING *""",
        uid,
    )

    resources = await _load_session_resources(uid)
    return _row_to_session(updated, resources=resources)


# ---- POST /v1/sessions/{id}/events ----


@router.get("/{session_id}/streaming")
async def get_session_streaming(session_id: str):
    """Get the in-flight streaming state for a session.

    Returns the accumulated text so far if the model is currently generating,
    or null if no active stream.
    """
    state = await get_stream(session_id)
    return {"streaming": state}


@router.get("/{session_id}/events", response_model=PaginatedEventsResponse)
async def get_session_events(
    session_id: str,
    after_cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    types: list[str] | None = Query(default=None, alias="types[]"),
) -> PaginatedEventsResponse:
    """Get events for a session with cursor-based pagination.

    v0.2.0 item #12 — optional ``?types[]=X&types[]=Y`` filter restricts the
    response to events whose ``type`` is in the list. Unknown type strings
    return 422 with the offender, rather than a silent empty result set.
    """
    try:
        uid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    row = await fetch_one("SELECT * FROM sessions WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    # `?types[]=` (no value) is parsed by Starlette as [""], not None. Treat
    # blank/whitespace entries as "no filter" so an empty query string behaves
    # the same as omitting the param — the documented contract.
    if types is not None:
        types = [t for t in types if t and t.strip()]
        if not types:
            types = None

    # Validate every requested type against EVENT_TYPES so we return a
    # clean 422 with the offender, rather than a silent empty result set.
    if types:
        from app.models import EVENT_TYPES
        invalid = [t for t in types if t not in EVENT_TYPES]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_event_type",
                    "message": (
                        f"unknown event type(s): {invalid!r}. "
                        f"Valid types: {sorted(EVENT_TYPES)}"
                    ),
                    "invalid": invalid,
                },
            )

    result = await get_events(session_id, after_cursor=after_cursor, limit=limit, types=types)
    return result


@router.post("/{session_id}/events", status_code=201, response_model=list[EventResponse])
async def post_events(session_id: str, body: PostEventsRequest) -> list[EventResponse]:
    """Post events to a session.

    Validates event types (422 via Pydantic), checks session exists (404),
    checks session is not archived (409), appends events, and triggers
    LISTEN/NOTIFY on the session channel.

    Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5, 10.4
    """
    # Validate session_id is a valid UUID
    try:
        uid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    # Check session exists
    row = await fetch_one("SELECT * FROM sessions WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    # Check session is not archived
    if row["archived_at"] is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "message": f"Session {session_id} is archived"},
        )

    # Append each event
    created_events: list[EventResponse] = []
    for ev in body.events:
        event = await append_event(session_id, ev.type, ev.payload)
        created_events.append(
            EventResponse(
                session_id=event.session_id,
                cursor=event.cursor,
                seq=event.seq,
                type=event.type,
                payload=event.payload,
                processed_at=event.processed_at,
            )
        )

    # Trigger LISTEN/NOTIFY — replace hyphens with underscores for PG channel name validity
    channel = f"session_{session_id.replace('-', '_')}"
    await notify(channel, "new_events")

    return created_events


# ---- GET /v1/sessions/{id}/stream ----


@router.get("/{session_id}/stream")
async def stream_session(session_id: str, cursor: str | None = Query(default=None)):
    """SSE stream endpoint: replay existing events then live-stream new ones.

    - Validates session exists (404 if not).
    - If cursor provided, validates it (422 if invalid).
    - Phase 1: Replay all events after the cursor (or from beginning).
    - Phase 2: Subscribe to PG LISTEN/NOTIFY and yield new events as they arrive.
    - On client disconnect, cleans up the LISTEN subscription.

    Validates: Requirements 7.1, 7.2, 7.4
    """
    # Validate session_id is a valid UUID
    try:
        uid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    # Check session exists
    row = await fetch_one("SELECT * FROM sessions WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    # Validate cursor if provided
    if cursor is not None:
        try:
            decode_cursor(cursor)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={"error": "validation_error", "message": f"Invalid cursor: {cursor}"},
            )

    async def event_generator():
        last_cursor = cursor

        # Phase 1: Replay existing events
        events_page = await get_events(session_id, after_cursor=last_cursor, limit=1000)
        for event in events_page.events:
            data = json.dumps({
                "session_id": event.session_id,
                "type": event.type,
                "cursor": event.cursor,
                "seq": event.seq,
                "payload": event.payload,
                "processed_at": str(event.processed_at) if event.processed_at else None,
            })
            yield {"data": data}
            last_cursor = event.cursor

        # Phase 2: Live stream via LISTEN/NOTIFY
        # Use a polling approach with LISTEN to avoid race conditions:
        # After subscribing, immediately check for events that arrived
        # between the end of Phase 1 and the LISTEN subscription.
        channel = f"session_{session_id.replace('-', '_')}"

        async for _payload in listen(channel):
            # On each notification (or initial subscription), fetch new events
            new_events = await get_events(session_id, after_cursor=last_cursor, limit=100)
            for event in new_events.events:
                data = json.dumps({
                    "session_id": event.session_id,
                    "type": event.type,
                    "cursor": event.cursor,
                    "seq": event.seq,
                    "payload": event.payload,
                    "processed_at": str(event.processed_at) if event.processed_at else None,
                })
                yield {"data": data}
                last_cursor = event.cursor

    return EventSourceResponse(event_generator())
