"""Unit tests for the Session Resources framework (v0.2.0 PR2).

Covers:
- SessionResourceConfig discriminator: every valid + reject shapes.
- mount_path validation: absolute, no '..', reserved prefixes.
- Within-request uniqueness of mount_path.
- POST /v1/sessions resources[] persistence:
    - type=file file_id existence + source=upload check.
    - memory_store / github_repository rejected as not_implemented.
- SessionResponse hydrates resources[] (always populated, empty list when none).

PR2 does NOT exercise sandbox mounting or live CRUD — those land in PR3 / PR4.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from app.models import (
    CreateSessionRequest,
    FileResource,
    GithubRepositoryResource,
    MemoryStoreResource,
    RESERVED_MOUNT_PREFIXES,
    SessionResourceConfig,
)


AUTH = {"Authorization": "Bearer test-secret-key"}


# ---------------------------------------------------------------------------
# Helpers (mirror test_sessions.py)
# ---------------------------------------------------------------------------


def _make_agent_row(*, agent_id: str | None = None):
    return {
        "id": uuid.UUID(agent_id) if agent_id else uuid.uuid4(),
        "name": "test-agent",
        "version": 1,
        "model": {"provider": "openrouter", "id": "anthropic/claude-sonnet-4", "base_url": None},
        "system": "You are helpful.",
        "tools": [],
        "mcp_servers": [],
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


def _make_env_row(*, env_id: str | None = None, net_type: str = "none"):
    return {
        "id": uuid.UUID(env_id) if env_id else uuid.uuid4(),
        "name": "test-env",
        "config": {"networking": {"type": net_type}},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


def _make_session_row(*, session_id: str | None = None, agent_id=None, environment_id=None):
    return {
        "id": uuid.UUID(session_id) if session_id else uuid.uuid4(),
        "agent_id": uuid.UUID(agent_id) if agent_id else uuid.uuid4(),
        "agent_version": 1,
        "environment_id": uuid.UUID(environment_id) if environment_id else uuid.uuid4(),
        "status": "running",
        "container_id": "container-abc",
        "title": None,
        "metadata": {},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "archived_at": None,
        "last_event_cursor": None,
        "ttl_seconds": None,
        "stats": {"total_events": 0, "tool_calls": 0, "model_turns": 0},
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "vault_ids": [],
    }


def _make_file_row(*, file_id: str | None = None, source: str = "upload", archived_at=None):
    return {
        "id": uuid.UUID(file_id) if file_id else uuid.uuid4(),
        "source": source,
        "archived_at": archived_at,
    }


def _make_resource_row(
    *,
    resource_id: str | None = None,
    session_id: str | None = None,
    mount_path: str = "/mnt/data/foo.csv",
    config: dict | None = None,
    state: str = "mounted",
):
    return {
        "id": uuid.UUID(resource_id) if resource_id else uuid.uuid4(),
        "session_id": uuid.UUID(session_id) if session_id else uuid.uuid4(),
        "type": "file",
        "mount_path": mount_path,
        "config": config or {"file_id": str(uuid.uuid4())},
        "state": state,
        "error": None,
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "unmounted_at": None,
    }


@pytest.fixture()
def sandbox_client():
    """TestClient with mocked sandbox + orchestrator (mirrors test_sessions fixture)."""
    with (
        patch("app.main.check_migrations_current"),
        patch("app.main.create_pool", new_callable=AsyncMock),
        patch("app.main.close_pool", new_callable=AsyncMock),
        patch("app.main.ensure_docker_networks", new_callable=AsyncMock),
        patch("app.main.DockerSandbox") as MockSandbox,
        patch("app.main.cleanup_expired_sessions", new_callable=AsyncMock),
        patch("app.main.recover_sessions", new_callable=AsyncMock),
        patch("app.routes.sessions.run_session", new_callable=AsyncMock),
    ):
        mock_sandbox = MagicMock()
        mock_sandbox.create = AsyncMock(return_value="container-abc")
        mock_sandbox.destroy = AsyncMock()
        MockSandbox.return_value = mock_sandbox

        from app.main import app

        with TestClient(app) as c:
            yield c, mock_sandbox


# ---------------------------------------------------------------------------
# Discriminator parsing
# ---------------------------------------------------------------------------


_ADAPTER = TypeAdapter(SessionResourceConfig)


class TestDiscriminator:
    def test_parses_file_resource(self):
        parsed = _ADAPTER.validate_python({
            "type": "file",
            "file_id": str(uuid.uuid4()),
            "mount_path": "/mnt/data/foo.csv",
        })
        assert isinstance(parsed, FileResource)
        assert parsed.mount_path == "/mnt/data/foo.csv"

    def test_parses_memory_store_resource(self):
        parsed = _ADAPTER.validate_python({
            "type": "memory_store",
            "memory_store_id": "mem_123",
        })
        assert isinstance(parsed, MemoryStoreResource)
        assert parsed.access == "read_only"  # default

    def test_parses_github_repository_resource(self):
        parsed = _ADAPTER.validate_python({
            "type": "github_repository",
            "url": "https://github.com/example/repo",
            "mount_path": "/mnt/repo",
        })
        assert isinstance(parsed, GithubRepositoryResource)

    def test_unknown_type_rejected(self):
        with pytest.raises(ValidationError):
            _ADAPTER.validate_python({"type": "vault", "id": "v_123"})

    def test_missing_type_rejected(self):
        with pytest.raises(ValidationError):
            _ADAPTER.validate_python({"file_id": "f_1", "mount_path": "/mnt/x"})

    def test_file_resource_missing_file_id_rejected(self):
        with pytest.raises(ValidationError):
            _ADAPTER.validate_python({"type": "file", "mount_path": "/mnt/x"})


# ---------------------------------------------------------------------------
# mount_path validation
# ---------------------------------------------------------------------------


class TestMountPathValidation:
    def test_relative_path_rejected(self):
        with pytest.raises(ValidationError, match="absolute"):
            FileResource(type="file", file_id=str(uuid.uuid4()), mount_path="data/foo")

    def test_empty_path_rejected(self):
        with pytest.raises(ValidationError):
            FileResource(type="file", file_id=str(uuid.uuid4()), mount_path="")

    @pytest.mark.parametrize("path", ["/a/../b", "/../etc", "/foo/../bar"])
    def test_traversal_rejected(self, path):
        with pytest.raises(ValidationError, match=r"\.\."):
            FileResource(type="file", file_id=str(uuid.uuid4()), mount_path=path)

    @pytest.mark.parametrize("prefix", RESERVED_MOUNT_PREFIXES)
    def test_reserved_prefixes_rejected(self, prefix):
        # Build a path that lives under the reserved prefix.
        path = prefix.rstrip("/") + "/some_file"
        with pytest.raises(ValidationError, match="reserved"):
            FileResource(type="file", file_id=str(uuid.uuid4()), mount_path=path)

    def test_proc_exact_match_rejected(self):
        # The bare /proc should be rejected, not only paths beneath it.
        with pytest.raises(ValidationError, match="reserved"):
            FileResource(type="file", file_id=str(uuid.uuid4()), mount_path="/proc")

    def test_lookalike_prefix_allowed(self):
        # /procfs is NOT reserved — only /proc and its descendants are.
        # Confirms prefix matching is segment-aware, not substring-based.
        ok = FileResource(type="file", file_id=str(uuid.uuid4()), mount_path="/procfs/data")
        assert ok.mount_path == "/procfs/data"

    def test_normal_path_accepted(self):
        ok = FileResource(type="file", file_id=str(uuid.uuid4()), mount_path="/mnt/data/foo.csv")
        assert ok.mount_path == "/mnt/data/foo.csv"

    def test_github_repository_mount_path_validated(self):
        with pytest.raises(ValidationError, match="reserved"):
            GithubRepositoryResource(
                type="github_repository",
                url="https://github.com/x/y",
                mount_path="/proc/repo",
            )


# ---------------------------------------------------------------------------
# Within-request uniqueness
# ---------------------------------------------------------------------------


class TestWithinRequestUniqueness:
    def test_duplicate_mount_path_rejected(self):
        f1_id = str(uuid.uuid4())
        f2_id = str(uuid.uuid4())
        with pytest.raises(ValidationError, match="duplicate mount_path"):
            CreateSessionRequest(
                agent_id=str(uuid.uuid4()),
                environment_id=str(uuid.uuid4()),
                resources=[
                    {"type": "file", "file_id": f1_id, "mount_path": "/mnt/data/x"},
                    {"type": "file", "file_id": f2_id, "mount_path": "/mnt/data/x"},
                ],
            )

    def test_distinct_mount_paths_accepted(self):
        req = CreateSessionRequest(
            agent_id=str(uuid.uuid4()),
            environment_id=str(uuid.uuid4()),
            resources=[
                {"type": "file", "file_id": str(uuid.uuid4()), "mount_path": "/mnt/data/a"},
                {"type": "file", "file_id": str(uuid.uuid4()), "mount_path": "/mnt/data/b"},
            ],
        )
        assert len(req.resources) == 2

    def test_memory_store_has_no_mount_path_collision(self):
        # memory_store has no mount_path; uniqueness check skips it cleanly.
        req = CreateSessionRequest(
            agent_id=str(uuid.uuid4()),
            environment_id=str(uuid.uuid4()),
            resources=[
                {"type": "file", "file_id": str(uuid.uuid4()), "mount_path": "/mnt/data/a"},
                {"type": "memory_store", "memory_store_id": "mem_1"},
                {"type": "memory_store", "memory_store_id": "mem_2"},
            ],
        )
        assert len(req.resources) == 3


# ---------------------------------------------------------------------------
# POST /v1/sessions — happy path with file resource
# ---------------------------------------------------------------------------


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_with_file_resource_persists(mock_fetch_one, mock_fetch_all, sandbox_client):
    """Happy path: session with one file resource inserts the row + returns it."""
    client, _sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    # fetch_one call order in create_session:
    #   1. agent lookup
    #   2. env lookup
    #   3. file lookup (per resource)
    #   4. INSERT sessions RETURNING *
    #   5. INSERT session_resources RETURNING * (per resource)
    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_file_row(file_id=file_id),
        _make_session_row(session_id=session_id, agent_id=agent_id, environment_id=env_id),
        _make_resource_row(
            session_id=session_id,
            mount_path="/mnt/data/report.pdf",
            config={"file_id": file_id},
        ),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": file_id, "mount_path": "/mnt/data/report.pdf"},
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["resources"]) == 1
    assert body["resources"][0]["type"] == "file"
    assert body["resources"][0]["mount_path"] == "/mnt/data/report.pdf"
    assert body["resources"][0]["state"] == "mounted"
    assert body["resources"][0]["config"]["file_id"] == file_id


# ---------------------------------------------------------------------------
# POST /v1/sessions — file existence / source / archive guardrails
# ---------------------------------------------------------------------------


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_file_not_found_returns_422(mock_fetch_one, mock_fetch_all, sandbox_client):
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        None,  # file lookup returns nothing
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": str(uuid.uuid4()), "mount_path": "/mnt/data/x"},
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "validation_error"


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_archived_file_returns_422(mock_fetch_one, mock_fetch_all, sandbox_client):
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_file_row(file_id=file_id, archived_at=datetime(2025, 1, 1, tzinfo=timezone.utc)),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": file_id, "mount_path": "/mnt/data/x"},
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)
    assert resp.status_code == 422
    assert "archived" in resp.json()["detail"]["message"]


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_deliverable_source_rejected(mock_fetch_one, mock_fetch_all, sandbox_client):
    """Deliverables cannot be re-mounted as resources (covert-channel guard)."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_file_row(file_id=file_id, source="deliverable"),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": file_id, "mount_path": "/mnt/data/x"},
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)
    assert resp.status_code == 422
    assert "uploads" in resp.json()["detail"]["message"]


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_invalid_file_uuid_rejected(mock_fetch_one, mock_fetch_all, sandbox_client):
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": "not-a-uuid", "mount_path": "/mnt/data/x"},
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Deferred types: memory_store + github_repository → 422 not_implemented
# ---------------------------------------------------------------------------


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_memory_store_resource_returns_not_implemented(
    mock_fetch_one, mock_fetch_all, sandbox_client
):
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "memory_store", "memory_store_id": "mem_1"},
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "not_implemented"


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_github_repository_resource_returns_not_implemented(
    mock_fetch_one, mock_fetch_all, sandbox_client
):
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {
                "type": "github_repository",
                "url": "https://github.com/x/y",
                "mount_path": "/mnt/repo",
            },
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "not_implemented"


# ---------------------------------------------------------------------------
# Response hydration
# ---------------------------------------------------------------------------


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_get_session_hydrates_resources(mock_fetch_one, mock_fetch_all, sandbox_client):
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    mock_fetch_one.return_value = _make_session_row(session_id=sid)
    mock_fetch_all.return_value = [
        _make_resource_row(session_id=sid, mount_path="/mnt/data/a"),
        _make_resource_row(session_id=sid, mount_path="/mnt/data/b"),
    ]
    resp = client.get(f"/v1/sessions/{sid}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    paths = [r["mount_path"] for r in body["resources"]]
    assert paths == ["/mnt/data/a", "/mnt/data/b"]


# ---------------------------------------------------------------------------
# Backwards compat — v0.1-shape create still works
# ---------------------------------------------------------------------------


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_without_resources_returns_empty_list(
    mock_fetch_one, mock_fetch_all, sandbox_client
):
    """A v0.1-shape request (no resources field, no Linchpin-API-Version header)
    succeeds and the response carries an empty resources[]."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_session_row(session_id=session_id, agent_id=agent_id, environment_id=env_id),
    ]
    mock_fetch_all.return_value = []

    # No `resources` field, no version header — pure v0.1 shape.
    payload = {"agent_id": agent_id, "environment_id": env_id}
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201
    body = resp.json()
    assert body["resources"] == []
