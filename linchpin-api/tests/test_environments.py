"""Unit tests for environment CRUD endpoints.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


AUTH = {"Authorization": "Bearer test-secret-key"}


def _make_env_row(
    *,
    env_id: str | None = None,
    name: str = "test-env",
    config: dict | None = None,
    archived_at=None,
):
    """Build a fake asyncpg Record-like dict for an environment row."""
    return {
        "id": uuid.UUID(env_id) if env_id else uuid.uuid4(),
        "name": name,
        "config": config or {"networking": {"type": "none"}},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "archived_at": archived_at,
    }


VALID_PAYLOAD = {
    "name": "my-env",
    "config": {"networking": {"type": "none"}},
}


# ---- POST /v1/environments ----


@patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
def test_create_environment_returns_201(mock_fetch, client):
    """A valid creation request returns 201 with the full environment resource."""
    mock_fetch.return_value = _make_env_row(name="my-env")

    resp = client.post("/v1/environments", json=VALID_PAYLOAD, headers=AUTH)

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "my-env"
    assert body["config"]["networking"]["type"] == "none"
    assert "id" in body
    assert "created_at" in body


@patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
def test_create_environment_unrestricted(mock_fetch, client):
    """Environment with unrestricted networking is accepted."""
    config = {"networking": {"type": "unrestricted"}}
    mock_fetch.return_value = _make_env_row(config=config)

    payload = {"name": "open-env", "config": config}
    resp = client.post("/v1/environments", json=payload, headers=AUTH)

    assert resp.status_code == 201
    assert resp.json()["config"]["networking"]["type"] == "unrestricted"


def test_create_environment_invalid_networking_returns_422(client):
    """Invalid networking type should be rejected with 422."""
    payload = {"name": "bad-env", "config": {"networking": {"type": "bridge"}}}
    resp = client.post("/v1/environments", json=payload, headers=AUTH)
    assert resp.status_code == 422


def test_create_environment_missing_name_returns_422(client):
    """Missing required field 'name' should be rejected with 422."""
    payload = {"config": {"networking": {"type": "none"}}}
    resp = client.post("/v1/environments", json=payload, headers=AUTH)
    assert resp.status_code == 422


def test_create_environment_missing_config_returns_422(client):
    """Missing required field 'config' should be rejected with 422."""
    payload = {"name": "no-config"}
    resp = client.post("/v1/environments", json=payload, headers=AUTH)
    assert resp.status_code == 422


def test_create_environment_missing_networking_returns_422(client):
    """Missing networking inside config should be rejected with 422."""
    payload = {"name": "bad", "config": {}}
    resp = client.post("/v1/environments", json=payload, headers=AUTH)
    assert resp.status_code == 422


# ---- GET /v1/environments/{id} ----


@patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
def test_get_environment_returns_200(mock_fetch, client):
    """Fetching an existing environment returns 200 with the full resource."""
    eid = str(uuid.uuid4())
    mock_fetch.return_value = _make_env_row(env_id=eid)

    resp = client.get(f"/v1/environments/{eid}", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["id"] == eid


@patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
def test_get_environment_not_found_returns_404(mock_fetch, client):
    """Fetching a non-existent environment returns 404."""
    mock_fetch.return_value = None
    eid = str(uuid.uuid4())

    resp = client.get(f"/v1/environments/{eid}", headers=AUTH)

    assert resp.status_code == 404
    body = resp.json()["detail"]
    assert body["error"] == "not_found"


def test_get_environment_invalid_uuid_returns_404(client):
    """Fetching with an invalid UUID returns 404."""
    resp = client.get("/v1/environments/not-a-uuid", headers=AUTH)
    assert resp.status_code == 404


# ---- GET /v1/environments ----


@patch("app.routes.environments.fetch_all", new_callable=AsyncMock)
def test_list_environments_returns_paginated(mock_fetch, client):
    """Listing environments returns a paginated response."""
    mock_fetch.return_value = [_make_env_row(), _make_env_row()]

    resp = client.get("/v1/environments", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["has_more"] is False


@patch("app.routes.environments.fetch_all", new_callable=AsyncMock)
def test_list_environments_has_more(mock_fetch, client):
    """When more environments exist than the limit, has_more is True."""
    mock_fetch.return_value = [_make_env_row() for _ in range(3)]

    resp = client.get("/v1/environments?limit=2", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["has_more"] is True


@patch("app.routes.environments.fetch_all", new_callable=AsyncMock)
def test_list_environments_empty(mock_fetch, client):
    """Listing environments when none exist returns empty list."""
    mock_fetch.return_value = []

    resp = client.get("/v1/environments", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["has_more"] is False


# ---- v0.2.0 item #6: archive + delete + list filter ----


@patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
def test_archive_environment_sets_archived_at(mock_fetch, client):
    """Happy path: POST .../archive flips a live env to archived."""
    eid = str(uuid.uuid4())
    archived_at = datetime(2026, 5, 15, tzinfo=timezone.utc)
    mock_fetch.side_effect = [
        _make_env_row(env_id=eid),                            # existing lookup
        _make_env_row(env_id=eid, archived_at=archived_at),   # UPDATE RETURNING *
    ]
    resp = client.post(f"/v1/environments/{eid}/archive", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is not None


@patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
def test_archive_environment_idempotent(mock_fetch, client):
    """Archiving an already-archived env returns the same row without
    re-stamping archived_at — single fetch_one call."""
    eid = str(uuid.uuid4())
    archived_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    mock_fetch.return_value = _make_env_row(env_id=eid, archived_at=archived_at)

    resp = client.post(f"/v1/environments/{eid}/archive", headers=AUTH)
    assert resp.status_code == 200
    assert mock_fetch.await_count == 1, "must not issue UPDATE on already-archived env"
    assert resp.json()["archived_at"] is not None


@patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
def test_archive_environment_404_when_missing(mock_fetch, client):
    mock_fetch.return_value = None
    resp = client.post(f"/v1/environments/{uuid.uuid4()}/archive", headers=AUTH)
    assert resp.status_code == 404


def test_archive_environment_404_for_malformed_id(client):
    resp = client.post("/v1/environments/not-a-uuid/archive", headers=AUTH)
    assert resp.status_code == 404


@patch("app.routes.environments.fetch_all", new_callable=AsyncMock)
def test_list_environments_excludes_archived_by_default(mock_fetch_all, client):
    """The list endpoint filters out archived envs unless include_archived=true.
    We verify by inspecting the SQL the route ran."""
    mock_fetch_all.return_value = []
    resp = client.get("/v1/environments", headers=AUTH)
    assert resp.status_code == 200
    sql = mock_fetch_all.await_args.args[0]
    assert "archived_at IS NULL" in sql, "default list must filter archived rows"


@patch("app.routes.environments.fetch_all", new_callable=AsyncMock)
def test_list_environments_include_archived_returns_all(mock_fetch_all, client):
    mock_fetch_all.return_value = []
    resp = client.get("/v1/environments?include_archived=true", headers=AUTH)
    assert resp.status_code == 200
    sql = mock_fetch_all.await_args.args[0]
    assert "archived_at IS NULL" not in sql, "include_archived must drop the filter"


@patch("app.routes.environments.execute", new_callable=AsyncMock)
@patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
def test_delete_environment_removes_row(mock_fetch, mock_execute, client):
    eid = str(uuid.uuid4())
    # fetch_one order: existence check → in-use check returns None
    mock_fetch.side_effect = [{"id": uuid.UUID(eid)}, None]

    resp = client.delete(f"/v1/environments/{eid}", headers=AUTH)
    assert resp.status_code == 204
    mock_execute.assert_awaited_once()


@patch("app.routes.environments.execute", new_callable=AsyncMock)
@patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
def test_delete_environment_409_when_session_active(mock_fetch, mock_execute, client):
    """Non-terminated sessions referencing this env block deletion."""
    eid = str(uuid.uuid4())
    sid = uuid.uuid4()
    mock_fetch.side_effect = [
        {"id": uuid.UUID(eid)},
        {"id": sid, "status": "running"},
    ]

    resp = client.delete(f"/v1/environments/{eid}", headers=AUTH)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "environment_in_use"
    assert detail["blocking_session_id"] == str(sid)
    assert detail["blocking_session_status"] == "running"
    mock_execute.assert_not_awaited()


@patch("app.routes.environments.fetch_one", new_callable=AsyncMock)
def test_delete_environment_404_when_missing(mock_fetch, client):
    mock_fetch.return_value = None
    resp = client.delete(f"/v1/environments/{uuid.uuid4()}", headers=AUTH)
    assert resp.status_code == 404


def test_delete_environment_404_for_malformed_id(client):
    resp = client.delete("/v1/environments/not-a-uuid", headers=AUTH)
    assert resp.status_code == 404


# ---- Auth required ----


def test_environments_endpoints_require_auth(client):
    """All environment endpoints should return 401 without auth."""
    assert client.get("/v1/environments").status_code == 401
    assert client.get(f"/v1/environments/{uuid.uuid4()}").status_code == 401
    assert client.post("/v1/environments", json=VALID_PAYLOAD).status_code == 401
    assert client.post(f"/v1/environments/{uuid.uuid4()}/archive").status_code == 401
    assert client.delete(f"/v1/environments/{uuid.uuid4()}").status_code == 401
