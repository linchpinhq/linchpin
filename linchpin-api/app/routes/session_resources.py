"""Session resources endpoints — read-only in v0.2.0 (PR3).

GET    /v1/sessions/{id}/resources              — List resources (boot-time + any post-mount-failure)
GET    /v1/sessions/{id}/resources/{sesrsc_id}  — Get a single resource
POST   /v1/sessions/{id}/resources              — 501: live mount deferred to v0.2.x (D1 + D2)
DELETE /v1/sessions/{id}/resources/{sesrsc_id}  — 501: live unmount deferred to v0.2.x (D1 + D2)

Per eng-review decision D2: routes are registered (rather than 404) so
SDKs probing them receive an unambiguous "not implemented yet" signal
instead of guessing whether the endpoint exists.

Live mount/unmount lands in a follow-on v0.2.x patch (item #1's PR4 in
the original 5-PR plan; deferred via D1).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException

from app.db import fetch_all, fetch_one
from app.models import (
    PaginatedListResponse,
    SessionResource,
    SessionResourceConfig,
)
from app.routes.sessions import _row_to_resource

logger = logging.getLogger("linchpin-api.session_resources")

router = APIRouter(
    prefix="/sessions/{session_id}/resources",
    tags=["session_resources"],
)


NOT_IMPLEMENTED_BODY = {
    "error": "not_implemented",
    "message": (
        "Live resource mount/unmount is not available in v0.2.0; this surface "
        "lands in a follow-on v0.2.x patch. Session-create accepts a "
        "`resources[]` array which is the v0.2.0 path for attaching files."
    ),
    "available_in": "v0.2.x",
}


def _parse_session_uuid(session_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )


def _parse_resource_uuid(resource_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(resource_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Session resource {resource_id} not found",
            },
        )


@router.get("", response_model=PaginatedListResponse)
async def list_session_resources(session_id: str) -> PaginatedListResponse:
    """List all resources persisted for ``session_id``.

    Includes every state ('mounted', 'failed', 'unmounted', 'unmounting').
    Callers wanting only active resources can filter on `state` client-side
    — server-side filtering lands with live mount/unmount in v0.2.x.
    """
    session_uid = _parse_session_uuid(session_id)

    # 404 distinctly when the session itself doesn't exist, so SDKs can
    # distinguish "no session" from "session has no resources".
    session_row = await fetch_one(
        "SELECT id FROM sessions WHERE id = $1", session_uid,
    )
    if session_row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Session {session_id} not found"},
        )

    rows = await fetch_all(
        """SELECT * FROM session_resources
           WHERE session_id = $1
           ORDER BY created_at ASC""",
        session_uid,
    )
    resources: list[SessionResource] = [_row_to_resource(r) for r in rows]
    return PaginatedListResponse(data=resources, has_more=False, next_cursor=None)


@router.get("/{resource_id}", response_model=SessionResource)
async def get_session_resource(session_id: str, resource_id: str) -> SessionResource:
    """Get a single session resource by id."""
    session_uid = _parse_session_uuid(session_id)
    resource_uid = _parse_resource_uuid(resource_id)

    row = await fetch_one(
        """SELECT * FROM session_resources
           WHERE id = $1 AND session_id = $2""",
        resource_uid,
        session_uid,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Session resource {resource_id} not found",
            },
        )
    return _row_to_resource(row)


@router.post("", status_code=501)
async def post_session_resource(
    session_id: str,
    body: SessionResourceConfig,
) -> dict:
    """Live resource mount — deferred to v0.2.x per eng-review D1.

    Returns 501 with a structured body identifying when the surface is
    expected. Body is parsed (and validated) so SDKs get clean 422 on
    malformed requests, then 501 if the request would otherwise be valid.
    """
    # Touch the path parameters so FastAPI registers them (and any future
    # auth dependency reads them for forbidden-vs-not-implemented decisions).
    _ = session_id
    _ = body
    raise HTTPException(status_code=501, detail=NOT_IMPLEMENTED_BODY)


@router.delete("/{resource_id}", status_code=501)
async def delete_session_resource(session_id: str, resource_id: str) -> dict:
    """Live resource unmount — deferred to v0.2.x per eng-review D1."""
    _ = session_id
    _ = resource_id
    raise HTTPException(status_code=501, detail=NOT_IMPLEMENTED_BODY)
