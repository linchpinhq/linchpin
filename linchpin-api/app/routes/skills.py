"""Skills API endpoints (v0.4.0).

POST   /v1/skills        — Upload a skill bundle (tar.gz or zip multipart)
GET    /v1/skills        — List skills; ``?include_archived=true`` opts in
GET    /v1/skills/{id}   — Metadata
DELETE /v1/skills/{id}   — Soft delete (sets archived_at)

The bundle bytes themselves are persisted under
``LINCHPIN_SKILLS_ROOT`` at a content-addressable path so identical
uploads dedupe. Re-uploading the same name overwrites the row's
bundle pointer (audit kept in updated_at).
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app.db import execute, fetch_all, fetch_one
from app.models import (
    SKILL_BUNDLE_MAX_BYTES,
    Skill,
    SkillListResponse,
)
from app.skills import (
    SkillBundleError,
    absolute_storage_path,
    parse_skill_metadata,
    storage_path_for,
)

logger = logging.getLogger("linchpin-api")

router = APIRouter(prefix="/skills", tags=["skills"])


# Self-host is single-tenant; the workspace_id column is a forward-
# looking placeholder. Mirrors memory_stores' approach until multi-
# tenancy lands (post-v1.0).
_DEFAULT_WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _row_to_skill(row: asyncpg.Record) -> Skill:
    return Skill(
        id=str(row["id"]),
        name=row["name"],
        description=row["description"],
        bundle_sha256=row["bundle_sha256"],
        bundle_size=row["bundle_size"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def _parse_uuid(value: str, label: str = "Skill") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"{label} {value} not found"},
        )


async def _persist_bundle(content: bytes, sha: str) -> str:
    """Write the bundle bytes to disk at the content-addressed path.

    Returns the relative storage_path. Idempotent — a second write of
    identical bytes is a no-op because the path is sha-derived.
    """
    rel = storage_path_for(sha)
    abs_path = absolute_storage_path(sha)
    if os.path.exists(abs_path):
        return rel  # dedupe — bytes already on disk
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    # Atomic write via tmp file + rename so a partial write doesn't
    # leave a corrupt path other uploads might dedupe to.
    tmp = abs_path + ".tmp"
    with open(tmp, "wb") as out:
        out.write(content)
    os.replace(tmp, abs_path)
    return rel


@router.post("", response_model=Skill, status_code=201)
async def upload_skill(bundle: UploadFile = File(...)) -> Skill:
    """Upload a skill bundle. Parses SKILL.md, validates the
    frontmatter, persists the archive on disk, and either inserts a
    new row or updates the existing one with the same ``name`` (the
    update path is how operators ship a new revision of a skill).
    """
    content = await bundle.read()
    if len(content) > SKILL_BUNDLE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "skill_too_large",
                "message": (
                    f"skill bundle exceeds {SKILL_BUNDLE_MAX_BYTES} byte cap"
                ),
                "limit_bytes": SKILL_BUNDLE_MAX_BYTES,
            },
        )

    try:
        name, description = parse_skill_metadata(content)
    except SkillBundleError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_skill_bundle", "message": str(exc)},
        )

    sha = hashlib.sha256(content).hexdigest()
    storage_rel = await _persist_bundle(content, sha)
    size_bytes = len(content)
    now = datetime.now(tz=timezone.utc)

    existing = await fetch_one(
        """
        SELECT * FROM skills
        WHERE workspace_id = $1 AND name = $2 AND archived_at IS NULL
        """,
        _DEFAULT_WORKSPACE,
        name,
    )
    if existing is not None:
        row = await fetch_one(
            """
            UPDATE skills
            SET description = $1, bundle_sha256 = $2, bundle_size = $3,
                storage_path = $4, updated_at = $5
            WHERE id = $6
            RETURNING *
            """,
            description,
            sha,
            size_bytes,
            storage_rel,
            now,
            existing["id"],
        )
    else:
        row = await fetch_one(
            """
            INSERT INTO skills
                (id, name, description, workspace_id, bundle_sha256,
                 bundle_size, storage_path, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)
            RETURNING *
            """,
            uuid.uuid4(),
            name,
            description,
            _DEFAULT_WORKSPACE,
            sha,
            size_bytes,
            storage_rel,
            now,
        )
    return _row_to_skill(row)


@router.get("", response_model=SkillListResponse)
async def list_skills(
    include_archived: bool = Query(default=False),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SkillListResponse:
    """List skills in the workspace. Live-only by default."""
    if include_archived:
        rows = await fetch_all(
            """
            SELECT * FROM skills
            WHERE workspace_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            _DEFAULT_WORKSPACE,
            limit + 1,
            offset,
        )
    else:
        rows = await fetch_all(
            """
            SELECT * FROM skills
            WHERE workspace_id = $1 AND archived_at IS NULL
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            _DEFAULT_WORKSPACE,
            limit + 1,
            offset,
        )
    has_more = len(rows) > limit
    items = rows[:limit]
    return SkillListResponse(
        data=[_row_to_skill(r) for r in items],
        has_more=has_more,
        next_cursor=None,
    )


@router.get("/{skill_id}", response_model=Skill)
async def get_skill(skill_id: str) -> Skill:
    uid = _parse_uuid(skill_id)
    row = await fetch_one("SELECT * FROM skills WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Skill {skill_id} not found"},
        )
    return _row_to_skill(row)


@router.delete("/{skill_id}", response_model=Skill)
async def delete_skill(skill_id: str) -> Skill:
    """Soft-delete a skill (sets ``archived_at``). The bundle stays on
    disk in case it's still referenced by an agent's ``skills[]`` from
    a prior version; GC of orphaned bundles is a future concern.
    """
    uid = _parse_uuid(skill_id)
    row = await fetch_one("SELECT * FROM skills WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Skill {skill_id} not found"},
        )
    if row["archived_at"] is not None:
        return _row_to_skill(row)  # idempotent
    updated = await fetch_one(
        """
        UPDATE skills SET archived_at = NOW(), updated_at = NOW()
        WHERE id = $1 RETURNING *
        """,
        uid,
    )
    return _row_to_skill(updated)
