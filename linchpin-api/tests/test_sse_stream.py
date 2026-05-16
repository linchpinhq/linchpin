"""Unit tests for SSE streaming endpoint GET /v1/sessions/{id}/stream.

Validates: Requirements 7.1, 7.2, 7.4
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.events import encode_cursor
from app.models import EventResponse, PaginatedEventsResponse


AUTH = {"Authorization": "Bearer test-secret-key"}


def _make_session_row(*, session_id: str | None = None, archived_at=None):
    """Build a fake session row."""
    return {
        "id": uuid.UUID(session_id) if session_id else uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "agent_version": 1,
        "environment_id": uuid.uuid4(),
        "status": "running",
        "container_id": "container-abc",
        "title": None,
        "metadata": {},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "archived_at": archived_at,
        "last_event_cursor": None,
        "ttl_seconds": None,
        "stats": {"total_events": 0, "tool_calls": 0, "model_turns": 0},
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _make_events_response(session_id: str, count: int, start_seq: int = 1) -> PaginatedEventsResponse:
    """Build a PaginatedEventsResponse with fake events."""
    events = [
        EventResponse(
            session_id=session_id,
            cursor=encode_cursor(start_seq + i),
            seq=start_seq + i,
            type="user.message",
            payload={"content": f"msg-{start_seq + i}"},
            processed_at=None,
        )
        for i in range(count)
    ]
    return PaginatedEventsResponse(events=events, next_cursor=None)


@pytest.fixture()
def sandbox_client():
    """Return a TestClient that skips DB/migration/Docker startup."""
    with (
        patch("app.main.check_migrations_current"),
        patch("app.main.create_pool", new_callable=AsyncMock),
        patch("app.main.close_pool", new_callable=AsyncMock),
        patch("app.main.ensure_docker_networks", new_callable=AsyncMock),
        patch("app.main.DockerSandbox") as MockSandbox,
        patch("app.main.cleanup_expired_sessions", new_callable=AsyncMock),
        patch("app.main.recover_sessions", new_callable=AsyncMock),
    ):
        mock_sandbox = MagicMock()
        mock_sandbox.create = AsyncMock(return_value="container-mock")
        mock_sandbox.destroy = AsyncMock()
        mock_sandbox.ensure_image = AsyncMock(
            side_effect=lambda *, base_image, packages: base_image
        )
        MockSandbox.return_value = mock_sandbox

        from app.main import app
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            yield c


# ---- Session not found → 404 ----


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_stream_session_not_found(mock_fetch, sandbox_client):
    """Streaming a non-existent session returns 404."""
    mock_fetch.return_value = None
    sid = str(uuid.uuid4())

    resp = sandbox_client.get(f"/v1/sessions/{sid}/stream", headers=AUTH)

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


def test_stream_session_invalid_uuid(sandbox_client):
    """Streaming with an invalid UUID returns 404."""
    resp = sandbox_client.get("/v1/sessions/not-a-uuid/stream", headers=AUTH)
    assert resp.status_code == 404


# ---- Invalid cursor → 422 ----


@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_stream_invalid_cursor_returns_422(mock_fetch, sandbox_client):
    """Providing an invalid cursor returns 422."""
    sid = str(uuid.uuid4())
    mock_fetch.return_value = _make_session_row(session_id=sid)

    resp = sandbox_client.get(
        f"/v1/sessions/{sid}/stream?cursor=bad-cursor!!!",
        headers=AUTH,
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "validation_error"


# ---- Auth required ----


def test_stream_requires_auth(sandbox_client):
    """SSE stream endpoint requires authentication."""
    sid = str(uuid.uuid4())
    resp = sandbox_client.get(f"/v1/sessions/{sid}/stream")
    assert resp.status_code == 401


# ---- Replay existing events (Phase 1) ----


@patch("app.routes.sessions.listen")
@patch("app.routes.sessions.get_events", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_stream_replays_existing_events(mock_fetch, mock_get_events, mock_listen, sandbox_client):
    """SSE stream replays existing events before switching to live mode."""
    sid = str(uuid.uuid4())
    mock_fetch.return_value = _make_session_row(session_id=sid)

    events_resp = _make_events_response(sid, 3)
    mock_get_events.return_value = events_resp

    # Make listen() return an async generator that yields nothing (we just test replay)
    async def _empty_listen(channel):
        return
        yield  # make it an async generator that immediately stops

    mock_listen.side_effect = _empty_listen

    resp = sandbox_client.get(f"/v1/sessions/{sid}/stream", headers=AUTH)

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")

    # Parse SSE data lines
    lines = resp.text.strip().split("\n")
    data_lines = [l for l in lines if l.startswith("data:")]

    assert len(data_lines) == 3
    for i, data_line in enumerate(data_lines):
        payload = json.loads(data_line[len("data:"):].strip())
        assert payload["type"] == "user.message"
        assert payload["cursor"] == encode_cursor(i + 1)
        assert payload["payload"] == {"content": f"msg-{i + 1}"}


# ---- Replay with cursor (Phase 1 with after_cursor) ----


@patch("app.routes.sessions.listen")
@patch("app.routes.sessions.get_events", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_stream_replays_from_cursor(mock_fetch, mock_get_events, mock_listen, sandbox_client):
    """SSE stream with cursor replays only events after that cursor."""
    sid = str(uuid.uuid4())
    mock_fetch.return_value = _make_session_row(session_id=sid)

    # Events after cursor (seq 3 onwards)
    events_resp = _make_events_response(sid, 2, start_seq=3)
    mock_get_events.return_value = events_resp

    async def _empty_listen(channel):
        return
        yield

    mock_listen.side_effect = _empty_listen

    cursor = encode_cursor(2)
    resp = sandbox_client.get(f"/v1/sessions/{sid}/stream?cursor={cursor}", headers=AUTH)

    assert resp.status_code == 200

    # Verify get_events was called with the cursor
    mock_get_events.assert_called_once_with(sid, after_cursor=cursor, limit=1000)

    data_lines = [l for l in resp.text.strip().split("\n") if l.startswith("data:")]
    assert len(data_lines) == 2


# ---- No cursor → replay from beginning ----


@patch("app.routes.sessions.listen")
@patch("app.routes.sessions.get_events", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_stream_no_cursor_replays_from_beginning(mock_fetch, mock_get_events, mock_listen, sandbox_client):
    """SSE stream without cursor replays all events from the beginning."""
    sid = str(uuid.uuid4())
    mock_fetch.return_value = _make_session_row(session_id=sid)

    events_resp = _make_events_response(sid, 2)
    mock_get_events.return_value = events_resp

    async def _empty_listen(channel):
        return
        yield

    mock_listen.side_effect = _empty_listen

    resp = sandbox_client.get(f"/v1/sessions/{sid}/stream", headers=AUTH)

    assert resp.status_code == 200
    mock_get_events.assert_called_once_with(sid, after_cursor=None, limit=1000)


# ---- Empty replay ----


@patch("app.routes.sessions.listen")
@patch("app.routes.sessions.get_events", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_stream_empty_replay(mock_fetch, mock_get_events, mock_listen, sandbox_client):
    """SSE stream with no existing events produces no replay data."""
    sid = str(uuid.uuid4())
    mock_fetch.return_value = _make_session_row(session_id=sid)

    mock_get_events.return_value = PaginatedEventsResponse(events=[], next_cursor=None)

    async def _empty_listen(channel):
        return
        yield

    mock_listen.side_effect = _empty_listen

    resp = sandbox_client.get(f"/v1/sessions/{sid}/stream", headers=AUTH)

    assert resp.status_code == 200
    # No data lines expected (only possible ping/empty lines)
    data_lines = [l for l in resp.text.strip().split("\n") if l.startswith("data:")]
    assert len(data_lines) == 0


# ---- SSE message format ----


@patch("app.routes.sessions.listen")
@patch("app.routes.sessions.get_events", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_stream_event_json_format(mock_fetch, mock_get_events, mock_listen, sandbox_client):
    """Each SSE event contains type, cursor, and payload fields in JSON."""
    sid = str(uuid.uuid4())
    mock_fetch.return_value = _make_session_row(session_id=sid)

    events_resp = PaginatedEventsResponse(
        events=[
            EventResponse(
                session_id=sid,
                cursor=encode_cursor(1),
                seq=1,
                type="agent.message",
                payload={"text": "hello world"},
                processed_at=None,
            )
        ],
        next_cursor=None,
    )
    mock_get_events.return_value = events_resp

    async def _empty_listen(channel):
        return
        yield

    mock_listen.side_effect = _empty_listen

    resp = sandbox_client.get(f"/v1/sessions/{sid}/stream", headers=AUTH)

    assert resp.status_code == 200
    data_lines = [l for l in resp.text.strip().split("\n") if l.startswith("data:")]
    assert len(data_lines) == 1

    event_data = json.loads(data_lines[0][len("data:"):].strip())
    assert event_data["type"] == "agent.message"
    assert event_data["cursor"] == encode_cursor(1)
    assert event_data["payload"] == {"text": "hello world"}


# ---- Live streaming (Phase 2) ----


@patch("app.routes.sessions.listen")
@patch("app.routes.sessions.get_events", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_stream_live_events_after_replay(mock_fetch, mock_get_events, mock_listen, sandbox_client):
    """After replay, live events from LISTEN/NOTIFY are streamed."""
    sid = str(uuid.uuid4())
    mock_fetch.return_value = _make_session_row(session_id=sid)

    # Phase 1: no replay events
    replay_resp = PaginatedEventsResponse(events=[], next_cursor=None)
    # Phase 2: one notification triggers fetching new events
    live_resp = _make_events_response(sid, 1, start_seq=1)

    mock_get_events.side_effect = [replay_resp, live_resp]

    async def _one_notification(channel):
        yield "new_events"

    mock_listen.side_effect = _one_notification

    resp = sandbox_client.get(f"/v1/sessions/{sid}/stream", headers=AUTH)

    assert resp.status_code == 200
    data_lines = [l for l in resp.text.strip().split("\n") if l.startswith("data:")]
    assert len(data_lines) == 1

    event_data = json.loads(data_lines[0][len("data:"):].strip())
    assert event_data["type"] == "user.message"
    assert event_data["cursor"] == encode_cursor(1)


# ---- LISTEN channel name ----


@patch("app.routes.sessions.listen")
@patch("app.routes.sessions.get_events", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_stream_listen_channel_uses_underscores(mock_fetch, mock_get_events, mock_listen, sandbox_client):
    """The LISTEN channel replaces hyphens with underscores in session_id."""
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    mock_fetch.return_value = _make_session_row(session_id=sid)
    mock_get_events.return_value = PaginatedEventsResponse(events=[], next_cursor=None)

    async def _empty_listen(channel):
        return
        yield

    mock_listen.side_effect = _empty_listen

    resp = sandbox_client.get(f"/v1/sessions/{sid}/stream", headers=AUTH)

    assert resp.status_code == 200
    expected_channel = f"session_{sid.replace('-', '_')}"
    mock_listen.assert_called_once_with(expected_channel)
