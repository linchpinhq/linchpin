"""Agent CRUD endpoints.

POST /v1/agents       — Create agent
GET  /v1/agents/{id}  — Get agent by id
GET  /v1/agents       — List agents (paginated)

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Query

from app.db import fetch_all, fetch_one, execute
from app.models import (
    AgentResponse,
    CreateAgentRequest,
    PaginatedListResponse,
    UpdateAgentRequest,
)

router = APIRouter(prefix="/agents", tags=["agents"])


def _row_to_agent(row) -> AgentResponse:
    """Convert an asyncpg Record to an AgentResponse."""
    return AgentResponse(
        id=str(row["id"]),
        name=row["name"],
        version=row["version"],
        model=json.loads(row["model"]) if isinstance(row["model"], str) else row["model"],
        system=row["system"],
        tools=json.loads(row["tools"]) if isinstance(row["tools"], str) else row["tools"],
        mcp_servers=json.loads(row["mcp_servers"]) if isinstance(row["mcp_servers"], str) else row["mcp_servers"],
        created_at=row["created_at"],
    )


@router.post("", status_code=201, response_model=AgentResponse)
async def create_agent(body: CreateAgentRequest) -> AgentResponse:
    """Create a new agent resource."""
    agent_id = str(uuid.uuid4())

    model_json = body.model.model_dump(mode="json")
    tools_json = [t.model_dump(mode="json") for t in body.tools]
    mcp_json = [m.model_dump(mode="json") for m in body.mcp_servers]

    row = await fetch_one(
        """
        INSERT INTO agents (id, name, model, system, tools, mcp_servers)
        VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, $6::jsonb)
        RETURNING *
        """,
        uuid.UUID(agent_id),
        body.name,
        json.dumps(model_json),
        body.system,
        json.dumps(tools_json),
        json.dumps(mcp_json),
    )

    return _row_to_agent(row)


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(agent_id: str) -> AgentResponse:
    """Retrieve an agent by id."""
    try:
        uid = uuid.UUID(agent_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Agent {agent_id} not found"},
        )

    row = await fetch_one("SELECT * FROM agents WHERE id = $1", uid)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Agent {agent_id} not found"},
        )
    return _row_to_agent(row)


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(agent_id: str, body: UpdateAgentRequest) -> AgentResponse:
    """Partially update an agent. Only provided fields are changed."""
    try:
        uid = uuid.UUID(agent_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Agent {agent_id} not found"},
        )

    # Build dynamic SET clause from non-None fields
    set_parts: list[str] = []
    params: list[object] = []
    idx = 1  # $1 is reserved for the WHERE id

    if body.name is not None:
        idx += 1
        set_parts.append(f"name = ${idx}")
        params.append(body.name)

    if body.model is not None:
        idx += 1
        set_parts.append(f"model = ${idx}::jsonb")
        params.append(json.dumps(body.model.model_dump(mode="json")))

    if body.system is not None:
        idx += 1
        set_parts.append(f"system = ${idx}")
        params.append(body.system)

    if body.tools is not None:
        idx += 1
        set_parts.append(f"tools = ${idx}::jsonb")
        params.append(json.dumps([t.model_dump(mode="json") for t in body.tools]))

    if body.mcp_servers is not None:
        idx += 1
        set_parts.append(f"mcp_servers = ${idx}::jsonb")
        params.append(json.dumps([m.model_dump(mode="json") for m in body.mcp_servers]))

    # Always bump version
    set_parts.append("version = version + 1")

    if not set_parts:
        # Nothing to update besides version bump
        pass

    query = f"UPDATE agents SET {', '.join(set_parts)} WHERE id = $1 RETURNING *"
    row = await fetch_one(query, uid, *params)

    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"Agent {agent_id} not found"},
        )

    return _row_to_agent(row)


@router.get("", response_model=PaginatedListResponse)
async def list_agents(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedListResponse:
    """Return a paginated list of agents."""
    rows = await fetch_all(
        "SELECT * FROM agents ORDER BY created_at DESC LIMIT $1 OFFSET $2",
        limit + 1,  # fetch one extra to detect has_more
        offset,
    )

    has_more = len(rows) > limit
    items = rows[:limit]

    agents = [_row_to_agent(r) for r in items]

    return PaginatedListResponse(
        data=agents,
        has_more=has_more,
        next_cursor=None,
    )
