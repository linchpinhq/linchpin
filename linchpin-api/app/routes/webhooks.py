"""Webhook endpoint CRUD + delivery audit endpoints (v0.2.0 item #14).

POST   /v1/webhook_endpoints                    — create (returns secret once)
GET    /v1/webhook_endpoints                    — list (paginated)
GET    /v1/webhook_endpoints/{id}               — get (no secret)
PATCH  /v1/webhook_endpoints/{id}               — update (optionally rotate secret)
DELETE /v1/webhook_endpoints/{id}               — archive (soft-delete)
GET    /v1/webhook_endpoints/{id}/deliveries    — list deliveries for an endpoint
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Query

from app.db import execute, fetch_all, fetch_one
from app.models import (
    CreateWebhookEndpointRequest,
    PaginatedListResponse,
    UpdateWebhookEndpointRequest,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.webhooks import generate_secret


router = APIRouter(prefix="/webhook_endpoints", tags=["webhooks"])


def _row_to_endpoint(row, *, include_secret: bool = False) -> WebhookEndpoint:
    enabled_events_raw = row["enabled_events"]
    enabled_events = (
        json.loads(enabled_events_raw)
        if isinstance(enabled_events_raw, str)
        else (enabled_events_raw or [])
    )
    return WebhookEndpoint(
        id=str(row["id"]),
        url=row["url"],
        enabled_events=enabled_events,
        description=row["description"],
        enabled=row["enabled"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
        secret=row["secret"] if include_secret else None,
    )


def _row_to_delivery(row) -> WebhookDelivery:
    return WebhookDelivery(
        id=str(row["id"]),
        endpoint_id=str(row["endpoint_id"]),
        event_type=row["event_type"],
        status=row["status"],
        attempts=row["attempts"],
        last_attempt_at=row["last_attempt_at"],
        last_status_code=row["last_status_code"],
        last_error=row["last_error"],
        next_attempt_at=row["next_attempt_at"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _parse_endpoint_uuid(endpoint_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(endpoint_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Webhook endpoint {endpoint_id} not found",
            },
        )


@router.post("", status_code=201, response_model=WebhookEndpoint)
async def create_webhook_endpoint(
    body: CreateWebhookEndpointRequest,
) -> WebhookEndpoint:
    """Create a new webhook endpoint. The HMAC secret is returned ONCE in
    this response — subsequent reads omit it. Use ``PATCH ... rotate_secret``
    to roll a forgotten one (invalidates the old)."""
    endpoint_id = uuid.uuid4()
    secret = generate_secret()
    enabled_events_json = json.dumps(body.enabled_events)

    row = await fetch_one(
        """
        INSERT INTO webhook_endpoints
            (id, url, secret, enabled_events, description, enabled)
        VALUES ($1, $2, $3, $4::jsonb, $5, true)
        RETURNING *
        """,
        endpoint_id,
        body.url,
        secret,
        enabled_events_json,
        body.description,
    )

    # The create response is the *only* place the secret leaves the DB
    # in clear. Read-after-write paths strip it.
    return _row_to_endpoint(row, include_secret=True)


@router.get("/{endpoint_id}", response_model=WebhookEndpoint)
async def get_webhook_endpoint(endpoint_id: str) -> WebhookEndpoint:
    uid = _parse_endpoint_uuid(endpoint_id)
    row = await fetch_one("SELECT * FROM webhook_endpoints WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Webhook endpoint {endpoint_id} not found",
            },
        )
    return _row_to_endpoint(row, include_secret=False)


@router.patch("/{endpoint_id}", response_model=WebhookEndpoint)
async def update_webhook_endpoint(
    endpoint_id: str, body: UpdateWebhookEndpointRequest
) -> WebhookEndpoint:
    uid = _parse_endpoint_uuid(endpoint_id)
    existing = await fetch_one(
        "SELECT * FROM webhook_endpoints WHERE id = $1", uid
    )
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Webhook endpoint {endpoint_id} not found",
            },
        )

    updates: dict = {}
    if body.url is not None:
        updates["url"] = body.url
    if body.enabled_events is not None:
        updates["enabled_events"] = json.dumps(body.enabled_events)
    if body.description is not None:
        updates["description"] = body.description
    if body.enabled is not None:
        updates["enabled"] = body.enabled

    new_secret: str | None = None
    if body.rotate_secret:
        new_secret = generate_secret()
        updates["secret"] = new_secret

    if not updates:
        # No-op patch. Return current state.
        return _row_to_endpoint(existing, include_secret=False)

    set_clauses: list[str] = []
    values: list = []
    for i, (col, val) in enumerate(updates.items(), start=1):
        if col == "enabled_events":
            set_clauses.append(f"{col} = ${i}::jsonb")
        else:
            set_clauses.append(f"{col} = ${i}")
        values.append(val)
    set_clauses.append("updated_at = NOW()")

    sql = (
        f"UPDATE webhook_endpoints SET {', '.join(set_clauses)} "
        f"WHERE id = ${len(values) + 1} RETURNING *"
    )
    values.append(uid)
    row = await fetch_one(sql, *values)
    return _row_to_endpoint(row, include_secret=new_secret is not None)


@router.delete("/{endpoint_id}", status_code=204)
async def delete_webhook_endpoint(endpoint_id: str) -> None:
    """Soft-delete (sets ``archived_at``). The endpoint stops receiving
    new deliveries but historical ``webhook_deliveries`` rows are
    preserved for audit. Idempotent."""
    uid = _parse_endpoint_uuid(endpoint_id)
    existing = await fetch_one(
        "SELECT id FROM webhook_endpoints WHERE id = $1", uid
    )
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"Webhook endpoint {endpoint_id} not found",
            },
        )
    await execute(
        "UPDATE webhook_endpoints SET archived_at = NOW(), enabled = false "
        "WHERE id = $1 AND archived_at IS NULL",
        uid,
    )


@router.get("", response_model=PaginatedListResponse)
async def list_webhook_endpoints(
    limit: int = Query(20, ge=1, le=100),
    include_archived: bool = Query(False),
) -> PaginatedListResponse:
    where = "" if include_archived else "WHERE archived_at IS NULL"
    rows = await fetch_all(
        f"SELECT * FROM webhook_endpoints {where} ORDER BY created_at DESC LIMIT $1",
        limit,
    )
    return PaginatedListResponse(
        data=[_row_to_endpoint(r, include_secret=False) for r in rows],
        next_cursor=None,
    )


@router.get("/{endpoint_id}/deliveries", response_model=PaginatedListResponse)
async def list_webhook_deliveries(
    endpoint_id: str,
    limit: int = Query(50, ge=1, le=200),
) -> PaginatedListResponse:
    uid = _parse_endpoint_uuid(endpoint_id)
    rows = await fetch_all(
        """
        SELECT * FROM webhook_deliveries
        WHERE endpoint_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        uid,
        limit,
    )
    return PaginatedListResponse(
        data=[_row_to_delivery(r) for r in rows],
        next_cursor=None,
    )
