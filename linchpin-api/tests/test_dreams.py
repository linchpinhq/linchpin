"""Tests for v0.7.0 PR1 — Dreams (async memory curation).

Covers:
- ``CreateDreamRequest`` + ``DreamSessionFilter`` validation.
- POST/GET/cancel round-trips against a mocked DB.
- Validator failure modes: missing input store, archived input
  store, no dreamer configured, archived dreamer, output-name collision.
- Cancel: terminal Dreams reject; non-terminal Dreams flip to
  ``canceled`` and best-effort terminate the underlying dreamer
  session.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models import (
    CreateDreamRequest,
    DreamSessionFilter,
    MAX_SESSIONS_PER_DREAM,
)


AUTH = {"Authorization": "Bearer test-secret-key"}


# ---------------------------------------------------------------------------
# DreamSessionFilter / CreateDreamRequest validation
# ---------------------------------------------------------------------------


class TestDreamSessionFilter:
    def test_defaults(self):
        f = DreamSessionFilter()
        assert f.agent_id is None
        assert f.limit == MAX_SESSIONS_PER_DREAM

    def test_limit_cap(self):
        with pytest.raises(Exception):
            DreamSessionFilter(limit=MAX_SESSIONS_PER_DREAM + 1)

    def test_limit_floor(self):
        with pytest.raises(Exception):
            DreamSessionFilter(limit=0)


class TestCreateDreamRequest:
    def test_happy_path(self):
        req = CreateDreamRequest.model_validate({
            "input_memory_store_id": str(uuid.uuid4()),
            "output_store_name": "curated-prefs",
        })
        assert req.session_filter.limit == MAX_SESSIONS_PER_DREAM

    def test_with_filter(self):
        req = CreateDreamRequest.model_validate({
            "input_memory_store_id": str(uuid.uuid4()),
            "output_store_name": "curated-prefs",
            "session_filter": {"agent_id": "agt_123", "limit": 10},
        })
        assert req.session_filter.agent_id == "agt_123"
        assert req.session_filter.limit == 10


# ---------------------------------------------------------------------------
# Route surface — fixture + helpers
# ---------------------------------------------------------------------------


def _make_store_row(*, archived: bool = False):
    return {
        "id": uuid.uuid4(),
        "archived_at": datetime(2025, 1, 1, tzinfo=timezone.utc) if archived else None,
    }


def _make_agent_row(*, agent_id=None, archived: bool = False):
    return {
        "id": uuid.UUID(agent_id) if agent_id else uuid.uuid4(),
        "archived_at": datetime(2025, 1, 1, tzinfo=timezone.utc) if archived else None,
    }


def _make_dream_row(
    *, dream_id=None, status="pending", dreamer_session_id=None,
    output_memory_store_id=None,
):
    return {
        "id": uuid.UUID(dream_id) if dream_id else uuid.uuid4(),
        "workspace_id": uuid.UUID("00000000-0000-0000-0000-000000000000"),
        "input_memory_store_id": uuid.uuid4(),
        "output_memory_store_id": (
            uuid.UUID(output_memory_store_id)
            if output_memory_store_id else None
        ),
        "output_store_name": "curated-prefs",
        "dreamer_session_id": (
            uuid.UUID(dreamer_session_id) if dreamer_session_id else None
        ),
        "dreamer_agent_id": uuid.uuid4(),
        "session_filter": {},
        "status": status,
        "error": None,
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "started_at": None,
        "ended_at": None,
    }


@pytest.fixture()
def app_client(tmp_path):
    with (
        patch("app.main.check_migrations_current"),
        patch("app.main.create_pool", new_callable=AsyncMock),
        patch("app.main.close_pool", new_callable=AsyncMock),
        patch("app.main.ensure_docker_networks", new_callable=AsyncMock),
        patch("app.main.DockerSandbox"),
        patch("app.main.cleanup_expired_sessions", new_callable=AsyncMock),
        patch("app.main.recover_sessions", new_callable=AsyncMock),
    ):
        from app.main import app
        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# POST /v1/dreams
# ---------------------------------------------------------------------------


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_start_dream_happy_path(mock_fetch_one, app_client, monkeypatch):
    dreamer_id = str(uuid.uuid4())
    input_id = str(uuid.uuid4())
    monkeypatch.setenv("LINCHPIN_DREAMER_AGENT_ID", dreamer_id)

    mock_fetch_one.side_effect = [
        _make_store_row(),                          # input store exists
        _make_agent_row(agent_id=dreamer_id),       # dreamer agent exists
        None,                                       # no output-name collision
        _make_dream_row(),                          # INSERT RETURNING
    ]
    resp = app_client.post(
        "/v1/dreams",
        json={
            "input_memory_store_id": input_id,
            "output_store_name": "curated-prefs",
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["output_store_name"] == "curated-prefs"


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_start_dream_input_store_missing_returns_422(mock_fetch_one, app_client, monkeypatch):
    monkeypatch.setenv("LINCHPIN_DREAMER_AGENT_ID", str(uuid.uuid4()))
    mock_fetch_one.return_value = None
    resp = app_client.post(
        "/v1/dreams",
        json={
            "input_memory_store_id": str(uuid.uuid4()),
            "output_store_name": "curated",
        },
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "input memory store" in resp.json()["detail"]["message"]


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_start_dream_input_store_archived_returns_422(mock_fetch_one, app_client, monkeypatch):
    monkeypatch.setenv("LINCHPIN_DREAMER_AGENT_ID", str(uuid.uuid4()))
    mock_fetch_one.return_value = _make_store_row(archived=True)
    resp = app_client.post(
        "/v1/dreams",
        json={
            "input_memory_store_id": str(uuid.uuid4()),
            "output_store_name": "curated",
        },
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "archived" in resp.json()["detail"]["message"]


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_start_dream_no_dreamer_configured_returns_422(mock_fetch_one, app_client, monkeypatch):
    monkeypatch.delenv("LINCHPIN_DREAMER_AGENT_ID", raising=False)
    mock_fetch_one.return_value = _make_store_row()
    resp = app_client.post(
        "/v1/dreams",
        json={
            "input_memory_store_id": str(uuid.uuid4()),
            "output_store_name": "curated",
        },
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "dreamer_not_configured"


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_start_dream_per_request_dreamer_override(mock_fetch_one, app_client, monkeypatch):
    """Even without the env var, passing ``dreamer_agent_id`` on the
    request lets the call succeed."""
    monkeypatch.delenv("LINCHPIN_DREAMER_AGENT_ID", raising=False)
    dreamer_id = str(uuid.uuid4())
    mock_fetch_one.side_effect = [
        _make_store_row(),
        _make_agent_row(agent_id=dreamer_id),
        None,
        _make_dream_row(),
    ]
    resp = app_client.post(
        "/v1/dreams",
        json={
            "input_memory_store_id": str(uuid.uuid4()),
            "output_store_name": "curated",
            "dreamer_agent_id": dreamer_id,
        },
        headers=AUTH,
    )
    assert resp.status_code == 201


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_start_dream_dreamer_missing_returns_422(mock_fetch_one, app_client, monkeypatch):
    monkeypatch.setenv("LINCHPIN_DREAMER_AGENT_ID", str(uuid.uuid4()))
    mock_fetch_one.side_effect = [
        _make_store_row(),
        None,  # dreamer not found
    ]
    resp = app_client.post(
        "/v1/dreams",
        json={
            "input_memory_store_id": str(uuid.uuid4()),
            "output_store_name": "curated",
        },
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "dreamer agent" in resp.json()["detail"]["message"]


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_start_dream_dreamer_archived_returns_422(mock_fetch_one, app_client, monkeypatch):
    dreamer_id = str(uuid.uuid4())
    monkeypatch.setenv("LINCHPIN_DREAMER_AGENT_ID", dreamer_id)
    mock_fetch_one.side_effect = [
        _make_store_row(),
        _make_agent_row(agent_id=dreamer_id, archived=True),
    ]
    resp = app_client.post(
        "/v1/dreams",
        json={
            "input_memory_store_id": str(uuid.uuid4()),
            "output_store_name": "curated",
        },
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "archived" in resp.json()["detail"]["message"]


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_start_dream_output_name_collision_returns_409(mock_fetch_one, app_client, monkeypatch):
    dreamer_id = str(uuid.uuid4())
    monkeypatch.setenv("LINCHPIN_DREAMER_AGENT_ID", dreamer_id)
    mock_fetch_one.side_effect = [
        _make_store_row(),
        _make_agent_row(agent_id=dreamer_id),
        {"id": uuid.uuid4()},  # existing output store with same name
    ]
    resp = app_client.post(
        "/v1/dreams",
        json={
            "input_memory_store_id": str(uuid.uuid4()),
            "output_store_name": "curated",
        },
        headers=AUTH,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "output_store_name_taken"


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_start_dream_invalid_input_uuid_returns_422(mock_fetch_one, app_client, monkeypatch):
    monkeypatch.setenv("LINCHPIN_DREAMER_AGENT_ID", str(uuid.uuid4()))
    resp = app_client.post(
        "/v1/dreams",
        json={
            "input_memory_store_id": "not-a-uuid",
            "output_store_name": "curated",
        },
        headers=AUTH,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/dreams + /v1/dreams/{id}
# ---------------------------------------------------------------------------


@patch("app.routes.dreams.fetch_all", new_callable=AsyncMock)
def test_list_dreams_newest_first(mock_fetch_all, app_client):
    mock_fetch_all.return_value = [_make_dream_row(), _make_dream_row()]
    resp = app_client.get("/v1/dreams", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    sql = mock_fetch_all.await_args.args[0]
    assert "ORDER BY created_at DESC" in sql


@patch("app.routes.dreams.fetch_all", new_callable=AsyncMock)
def test_list_dreams_status_filter(mock_fetch_all, app_client):
    mock_fetch_all.return_value = [_make_dream_row(status="running")]
    resp = app_client.get("/v1/dreams?status=running", headers=AUTH)
    assert resp.status_code == 200
    sql, _ws, status, _limit, _offset = mock_fetch_all.await_args.args
    assert status == "running"
    assert "AND status = $2" in sql


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_get_dream_returns_row(mock_fetch_one, app_client):
    row = _make_dream_row()
    mock_fetch_one.return_value = row
    resp = app_client.get(f"/v1/dreams/{row['id']}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["id"] == str(row["id"])


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_get_dream_not_found(mock_fetch_one, app_client):
    mock_fetch_one.return_value = None
    resp = app_client.get(f"/v1/dreams/{uuid.uuid4()}", headers=AUTH)
    assert resp.status_code == 404


def test_get_dream_invalid_uuid_returns_404(app_client):
    resp = app_client.get("/v1/dreams/not-a-uuid", headers=AUTH)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /v1/dreams/{id}/cancel
# ---------------------------------------------------------------------------


@patch("app.routes.dreams.execute", new_callable=AsyncMock)
@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_cancel_running_dream(mock_fetch_one, mock_execute, app_client):
    """A running dream flips to canceled AND the dreamer session is
    best-effort terminated."""
    dreamer_sid = str(uuid.uuid4())
    row = _make_dream_row(status="running", dreamer_session_id=dreamer_sid)
    canceled = dict(row, status="canceled",
                    ended_at=datetime(2025, 1, 2, tzinfo=timezone.utc))
    mock_fetch_one.side_effect = [row, canceled]
    resp = app_client.post(f"/v1/dreams/{row['id']}/cancel", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "canceled"
    # Session terminate UPDATE was issued.
    assert any(
        "UPDATE sessions SET status = 'terminated'" in c.args[0]
        for c in mock_execute.await_args_list
    )


@patch("app.routes.dreams.execute", new_callable=AsyncMock)
@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_cancel_pending_dream(mock_fetch_one, mock_execute, app_client):
    """Pending dreams don't have a dreamer session yet — cancel still
    works and only the dreams row is updated."""
    row = _make_dream_row(status="pending", dreamer_session_id=None)
    canceled = dict(row, status="canceled",
                    ended_at=datetime(2025, 1, 2, tzinfo=timezone.utc))
    mock_fetch_one.side_effect = [row, canceled]
    resp = app_client.post(f"/v1/dreams/{row['id']}/cancel", headers=AUTH)
    assert resp.status_code == 200
    # No session terminate, no execute calls beyond the dream UPDATE
    # (which goes through fetch_one returning the updated row, not execute).
    mock_execute.assert_not_called()


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_cancel_completed_dream_returns_409(mock_fetch_one, app_client):
    mock_fetch_one.return_value = _make_dream_row(status="completed")
    resp = app_client.post(
        f"/v1/dreams/{uuid.uuid4()}/cancel", headers=AUTH,
    )
    assert resp.status_code == 409


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_cancel_canceled_dream_returns_409(mock_fetch_one, app_client):
    mock_fetch_one.return_value = _make_dream_row(status="canceled")
    resp = app_client.post(
        f"/v1/dreams/{uuid.uuid4()}/cancel", headers=AUTH,
    )
    assert resp.status_code == 409


@patch("app.routes.dreams.fetch_one", new_callable=AsyncMock)
def test_cancel_dream_not_found(mock_fetch_one, app_client):
    mock_fetch_one.return_value = None
    resp = app_client.post(
        f"/v1/dreams/{uuid.uuid4()}/cancel", headers=AUTH,
    )
    assert resp.status_code == 404
