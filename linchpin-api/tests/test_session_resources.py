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


def _make_file_row(
    *,
    file_id: str | None = None,
    source: str = "upload",
    archived_at=None,
    storage_path: str = "aa/bb/abcd1234",
):
    return {
        "id": uuid.UUID(file_id) if file_id else uuid.uuid4(),
        "source": source,
        "archived_at": archived_at,
        "storage_path": storage_path,
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
def sandbox_client(tmp_path):
    """TestClient with mocked sandbox + orchestrator (mirrors test_sessions fixture).

    Patches:
    - PR3 mount-build: ``os.path.isfile`` (True default), ``get_file_store`` (stub).
    - PR5 deliverables: ``ensure_session_outputs_dir`` (writes to tmp_path),
      ``watch_session_deliverables`` (no-op AsyncMock so no real watcher task spawns).

    Individual tests can override any of these via nested ``with patch(...)``
    blocks to exercise specific branches.
    """
    fake_store = MagicMock()
    fake_store.absolute_path = lambda sp: f"/var/lib/linchpin/files/{sp}"

    def _fake_ensure(sid):
        p = tmp_path / "session-outputs" / sid
        p.mkdir(parents=True, exist_ok=True)
        return p

    with (
        patch("app.main.check_migrations_current"),
        patch("app.main.create_pool", new_callable=AsyncMock),
        patch("app.main.close_pool", new_callable=AsyncMock),
        patch("app.main.ensure_docker_networks", new_callable=AsyncMock),
        patch("app.main.DockerSandbox") as MockSandbox,
        patch("app.main.cleanup_expired_sessions", new_callable=AsyncMock),
        patch("app.main.recover_sessions", new_callable=AsyncMock),
        patch("app.routes.sessions.run_session", new_callable=AsyncMock),
        patch("app.routes.sessions.get_file_store", return_value=fake_store),
        patch("app.routes.sessions.os.path.isfile", return_value=True),
        patch("app.routes.sessions.append_event", new_callable=AsyncMock),
        patch("app.routes.sessions.ensure_session_outputs_dir", side_effect=_fake_ensure),
        patch("app.routes.sessions.watch_session_deliverables", new_callable=AsyncMock),
    ):
        mock_sandbox = MagicMock()
        mock_sandbox.create = AsyncMock(return_value="container-abc")
        mock_sandbox.destroy = AsyncMock()
        mock_sandbox.ensure_image = AsyncMock(
            side_effect=lambda *, base_image, packages: base_image
        )
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

    def test_double_slash_rejected(self):
        with pytest.raises(ValidationError, match="consecutive slashes"):
            FileResource(type="file", file_id=str(uuid.uuid4()), mount_path="/mnt//data")

    def test_trailing_slash_normalized(self):
        # Trailing slash stripped so the DB UNIQUE(session_id, mount_path) sees
        # /mnt/data and /mnt/data/ as the same logical mount.
        r = FileResource(type="file", file_id=str(uuid.uuid4()), mount_path="/mnt/data/")
        assert r.mount_path == "/mnt/data"

    def test_root_path_rejected(self):
        with pytest.raises(ValidationError, match="filesystem root"):
            FileResource(type="file", file_id=str(uuid.uuid4()), mount_path="/")


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
    # 501 Not Implemented is the correct HTTP status — the request is well-
    # formed; the server just doesn't implement this resource type yet.
    assert resp.status_code == 501
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
    assert resp.status_code == 501
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


# ---------------------------------------------------------------------------
# Multi-resource persistence
# ---------------------------------------------------------------------------


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_with_multiple_file_resources(mock_fetch_one, mock_fetch_all, sandbox_client):
    """Multiple file resources all persist in order."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    file_a = str(uuid.uuid4())
    file_b = str(uuid.uuid4())
    sid = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        # Distinct storage_paths — two files with different content. Sharing
        # storage_path would (correctly) 422 with duplicate_mount_source.
        _make_file_row(file_id=file_a, storage_path="aa/bb/contentA"),
        _make_file_row(file_id=file_b, storage_path="cc/dd/contentB"),
        _make_session_row(session_id=sid, agent_id=agent_id, environment_id=env_id),
        _make_resource_row(session_id=sid, mount_path="/mnt/data/a", config={"file_id": file_a}),
        _make_resource_row(session_id=sid, mount_path="/mnt/data/b", config={"file_id": file_b}),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": file_a, "mount_path": "/mnt/data/a"},
            {"type": "file", "file_id": file_b, "mount_path": "/mnt/data/b"},
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    paths = [r["mount_path"] for r in body["resources"]]
    assert paths == ["/mnt/data/a", "/mnt/data/b"]


# ---------------------------------------------------------------------------
# List batch attribution — resources must not leak across sessions
# ---------------------------------------------------------------------------


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
def test_list_sessions_attributes_resources_correctly(mock_fetch_all, sandbox_client):
    """Each session in a list response gets only its own resources, not another
    session's. Guards against a regression in the batch-attribution loop."""
    client, _ = sandbox_client
    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())

    session_a = _make_session_row(session_id=sid_a)
    session_b = _make_session_row(session_id=sid_b)

    # Three resources: two on A, one on B
    res_a1 = _make_resource_row(session_id=sid_a, mount_path="/mnt/a1")
    res_a2 = _make_resource_row(session_id=sid_a, mount_path="/mnt/a2")
    res_b1 = _make_resource_row(session_id=sid_b, mount_path="/mnt/b1")

    # fetch_all called twice: sessions list, then resources batch
    mock_fetch_all.side_effect = [
        [session_a, session_b],
        [res_a1, res_a2, res_b1],
    ]

    resp = client.get("/v1/sessions", headers=AUTH)
    assert resp.status_code == 200
    sessions = {s["id"]: s for s in resp.json()["data"]}
    assert sorted(r["mount_path"] for r in sessions[sid_a]["resources"]) == ["/mnt/a1", "/mnt/a2"]
    assert [r["mount_path"] for r in sessions[sid_b]["resources"]] == ["/mnt/b1"]


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
def test_list_sessions_empty_skips_resource_query(mock_fetch_all, sandbox_client):
    """When there are zero sessions, the resources batch query is skipped (the
    `if session_ids:` guard). Asserting call_count == 1 prevents a regression
    that drops the guard and fires a degenerate `WHERE session_id = ANY(ARRAY[])`."""
    client, _ = sandbox_client
    mock_fetch_all.side_effect = [[]]

    resp = client.get("/v1/sessions", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["data"] == []
    assert mock_fetch_all.call_count == 1, "resources query should be skipped on empty list"


# ---------------------------------------------------------------------------
# Hydration on update / terminate / archive (T10)
# ---------------------------------------------------------------------------


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_update_session_hydrates_resources(mock_fetch_one, mock_fetch_all, sandbox_client):
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    existing = _make_session_row(session_id=sid)
    updated = {**existing, "title": "New Title"}
    mock_fetch_one.side_effect = [existing, updated]
    mock_fetch_all.return_value = [_make_resource_row(session_id=sid, mount_path="/mnt/x")]

    resp = client.post(f"/v1/sessions/{sid}", json={"title": "New Title"}, headers=AUTH)
    assert resp.status_code == 200
    assert [r["mount_path"] for r in resp.json()["resources"]] == ["/mnt/x"]


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_terminate_session_hydrates_resources(mock_fetch_one, mock_fetch_all, sandbox_client):
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    existing = _make_session_row(session_id=sid)
    terminated = {**existing, "status": "terminated"}
    mock_fetch_one.side_effect = [existing, terminated]
    mock_fetch_all.return_value = [_make_resource_row(session_id=sid, mount_path="/mnt/x")]

    # PR3 — terminate also UPDATEs session_resources state via execute(); patch it.
    with patch("app.routes.sessions.execute", new_callable=AsyncMock):
        resp = client.delete(f"/v1/sessions/{sid}", headers=AUTH)
    assert resp.status_code == 200
    assert [r["mount_path"] for r in resp.json()["resources"]] == ["/mnt/x"]


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_archive_session_hydrates_resources(mock_fetch_one, mock_fetch_all, sandbox_client):
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    existing = _make_session_row(session_id=sid)
    archived = {**existing, "archived_at": datetime(2025, 6, 1, tzinfo=timezone.utc)}
    mock_fetch_one.side_effect = [existing, archived]
    mock_fetch_all.return_value = [_make_resource_row(session_id=sid, mount_path="/mnt/x")]

    resp = client.post(f"/v1/sessions/{sid}/archive", headers=AUTH)
    assert resp.status_code == 200
    assert [r["mount_path"] for r in resp.json()["resources"]] == ["/mnt/x"]


# ---------------------------------------------------------------------------
# PR3 — sandbox mount wiring + mount-failure path
# ---------------------------------------------------------------------------


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_passes_mounts_to_sandbox(
    mock_fetch_one, mock_fetch_all, sandbox_client
):
    """PR3 — sandbox.create receives a ResourceMount list built from the
    file resources, with host_path = file_store.absolute_path(storage_path)
    and container_path = resource.mount_path."""
    from app.sandbox import ResourceMount

    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    sid = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_file_row(file_id=file_id, storage_path="aa/bb/cafebabe"),
        _make_session_row(session_id=sid, agent_id=agent_id, environment_id=env_id),
        _make_resource_row(
            session_id=sid, mount_path="/mnt/data/report.pdf", config={"file_id": file_id},
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
    # sandbox.create called with mounts kwarg. PR5 adds a writable /mnt/session/outputs
    # bind alongside the file-resource :ro bind, so we look up the file mount by
    # container_path rather than asserting list shape.
    call = mock_sandbox.create.call_args
    mounts = call.kwargs.get("mounts")
    assert mounts is not None
    file_mounts = [m for m in mounts if m.container_path == "/mnt/data/report.pdf"]
    assert file_mounts == [ResourceMount(
        host_path="/var/lib/linchpin/files/aa/bb/cafebabe",
        container_path="/mnt/data/report.pdf",
        mode="ro",
    )]
    # And the PR5 writable outputs bind is always present.
    outputs_mounts = [m for m in mounts if m.container_path == "/mnt/session/outputs"]
    assert len(outputs_mounts) == 1
    assert outputs_mounts[0].mode == "rw"


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_marks_resource_failed_when_host_path_missing(
    mock_fetch_one, mock_fetch_all, sandbox_client
):
    """PR3 — when the backing FileStore path is missing on disk between
    validation and mount, the resource row is inserted with state='failed'
    and a session.resource_mount_failed event is emitted. Session still
    starts (per spec line 336)."""
    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    rid = uuid.uuid4()

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_file_row(file_id=file_id),
        _make_session_row(session_id=sid, agent_id=agent_id, environment_id=env_id),
        _make_resource_row(
            resource_id=str(rid),
            session_id=sid,
            mount_path="/mnt/data/ghost.csv",
            config={"file_id": file_id},
            state="failed",
        ),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": file_id, "mount_path": "/mnt/data/ghost.csv"},
        ],
    }
    # Force the disk re-check to claim the host path is missing.
    with patch("app.routes.sessions.os.path.isfile", return_value=False):
        resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["resources"][0]["state"] == "failed"

    # The failed file resource is NOT in the mounts list (skipped, not
    # propagated to docker). The writable outputs bind (PR5) IS still there.
    call = mock_sandbox.create.call_args
    mounts = call.kwargs.get("mounts")
    file_mounts = [m for m in mounts if m.container_path == "/mnt/data/ghost.csv"]
    assert file_mounts == [], "failed resource must not appear in mounts"
    outputs_mounts = [m for m in mounts if m.container_path == "/mnt/session/outputs"]
    assert len(outputs_mounts) == 1

    # And a session.resource_mount_failed event was emitted.
    # (We skip asserting args[0] == sid because the route generates the
    # session_id locally before INSERT; in production the mocked _make_session_row
    # would echo back that same UUID via RETURNING, but the mock returns whatever
    # _make_session_row was configured to return regardless of input. The meaningful
    # checks are event-type, resource_id, mount_path, and error.)
    from app.routes.sessions import append_event as patched_append_event
    patched_append_event.assert_awaited()
    args, kwargs = patched_append_event.call_args
    # append_event(session_id, event_type, payload)
    assert args[1] == "session.resource_mount_failed"
    assert args[2]["resource_id"] == str(rid)
    assert args[2]["mount_path"] == "/mnt/data/ghost.csv"
    assert "missing on disk" in args[2]["error"]


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_returns_500_when_sandbox_create_fails(
    mock_fetch_one, mock_fetch_all, sandbox_client
):
    """PR3 — total container-create failure (e.g., docker daemon down) is
    not partially recoverable; the route raises 500 rather than producing
    a half-built session."""
    from app.sandbox import SandboxError

    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_file_row(file_id=file_id),
    ]
    mock_fetch_all.return_value = []
    mock_sandbox.create.side_effect = SandboxError("docker daemon unavailable")

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": file_id, "mount_path": "/mnt/data/x"},
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"] == "sandbox_create_failed"


# ---------------------------------------------------------------------------
# PR3 — /v1/sessions/{id}/resources endpoints (D2)
# ---------------------------------------------------------------------------


@patch("app.routes.session_resources.fetch_all", new_callable=AsyncMock)
@patch("app.routes.session_resources.fetch_one", new_callable=AsyncMock)
def test_list_session_resources_returns_rows(
    mock_fetch_one, mock_fetch_all, sandbox_client
):
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    mock_fetch_one.return_value = {"id": uuid.UUID(sid)}
    mock_fetch_all.return_value = [
        _make_resource_row(session_id=sid, mount_path="/mnt/a"),
        _make_resource_row(session_id=sid, mount_path="/mnt/b"),
    ]
    resp = client.get(f"/v1/sessions/{sid}/resources", headers=AUTH)
    assert resp.status_code == 200
    paths = [r["mount_path"] for r in resp.json()["data"]]
    assert paths == ["/mnt/a", "/mnt/b"]


@patch("app.routes.session_resources.fetch_one", new_callable=AsyncMock)
def test_list_session_resources_404_when_session_missing(mock_fetch_one, sandbox_client):
    client, _ = sandbox_client
    mock_fetch_one.return_value = None
    resp = client.get(f"/v1/sessions/{uuid.uuid4()}/resources", headers=AUTH)
    assert resp.status_code == 404


def test_list_session_resources_404_when_session_id_malformed(sandbox_client):
    client, _ = sandbox_client
    resp = client.get("/v1/sessions/not-a-uuid/resources", headers=AUTH)
    assert resp.status_code == 404


@patch("app.routes.session_resources.fetch_one", new_callable=AsyncMock)
def test_get_session_resource_returns_row(mock_fetch_one, sandbox_client):
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    mock_fetch_one.return_value = _make_resource_row(
        resource_id=rid, session_id=sid, mount_path="/mnt/single",
    )
    resp = client.get(f"/v1/sessions/{sid}/resources/{rid}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["mount_path"] == "/mnt/single"


@patch("app.routes.session_resources.fetch_one", new_callable=AsyncMock)
def test_get_session_resource_404_when_missing(mock_fetch_one, sandbox_client):
    client, _ = sandbox_client
    mock_fetch_one.return_value = None
    resp = client.get(
        f"/v1/sessions/{uuid.uuid4()}/resources/{uuid.uuid4()}", headers=AUTH,
    )
    assert resp.status_code == 404


def test_post_session_resource_returns_501(sandbox_client):
    """D2 — live add deferred to v0.2.x; route returns 501 with structured body."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    resp = client.post(
        f"/v1/sessions/{sid}/resources",
        json={"type": "file", "file_id": file_id, "mount_path": "/mnt/late"},
        headers=AUTH,
    )
    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert detail["error"] == "not_implemented"
    assert detail["available_in"] == "v0.2.x"


def test_post_session_resource_422_for_invalid_body(sandbox_client):
    """Malformed POST body fails 422 before the 501 check fires —
    SDKs get clean validation errors instead of misleading 'not_implemented'."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    resp = client.post(
        f"/v1/sessions/{sid}/resources",
        json={"type": "file"},  # missing file_id + mount_path
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_delete_session_resource_returns_501(sandbox_client):
    """D2 — live unmount deferred to v0.2.x; route returns 501."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    resp = client.delete(
        f"/v1/sessions/{sid}/resources/{rid}", headers=AUTH,
    )
    assert resp.status_code == 501
    assert resp.json()["detail"]["error"] == "not_implemented"


# ---------------------------------------------------------------------------
# PR3 — post-review fixes: collision, CHECK constraint, UUID canonicalization,
# orphan-container rollback. Each test exercises a regression that the
# original implementation would have hit on first contact with a real DB
# or a real second mount.
# ---------------------------------------------------------------------------


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_422_when_two_resources_share_storage_path(
    mock_fetch_one, mock_fetch_all, sandbox_client
):
    """Two FileResources whose host_paths resolve to the same storage_path
    (content-addressed dedup) must be rejected with 422 — otherwise the
    docker-py ``volumes`` dict-key collision would silently drop one mount.
    """
    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    file_id_a = str(uuid.uuid4())
    file_id_b = str(uuid.uuid4())

    # Both file rows return identical storage_path → same host_path after
    # FileStore.absolute_path. The PR3 fix detects this in the route loop
    # before sandbox.create runs.
    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_file_row(file_id=file_id_a, storage_path="aa/bb/sharedhash"),
        _make_file_row(file_id=file_id_b, storage_path="aa/bb/sharedhash"),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": file_id_a, "mount_path": "/mnt/a"},
            {"type": "file", "file_id": file_id_b, "mount_path": "/mnt/b"},
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "duplicate_mount_source"
    # sandbox.create must NOT have been called — collision detected pre-create.
    mock_sandbox.create.assert_not_called()


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_persists_canonical_uuid_in_config(
    mock_fetch_one, mock_fetch_all, sandbox_client
):
    """A file_id submitted in non-canonical form (no dashes, uppercase) must
    be normalized to canonical lowercase-dashed form in JSONB config so the
    DELETE /v1/files 409 mount-conflict check (which queries with
    ``str(file_uid)`` — always canonical) actually fires."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    # Same UUID in two forms: canonical for the DB row, non-canonical for the POST body.
    canonical = str(uuid.uuid4())
    non_canonical = canonical.replace("-", "").upper()
    assert non_canonical != canonical  # sanity: forms differ
    sid = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_file_row(file_id=canonical, storage_path="aa/bb/canon"),
        _make_session_row(session_id=sid, agent_id=agent_id, environment_id=env_id),
        _make_resource_row(session_id=sid, mount_path="/mnt/d", config={"file_id": canonical}),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": non_canonical, "mount_path": "/mnt/d"},
        ],
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201, resp.text
    # Inspect the INSERT INTO session_resources call: 4th positional is config_json.
    insert_call = next(
        c for c in mock_fetch_one.call_args_list
        if c.args and "INSERT INTO session_resources" in c.args[0]
    )
    config_json = insert_call.args[4]
    config_obj = json.loads(config_json)
    assert config_obj["file_id"] == canonical, (
        f"file_id stored as {config_obj['file_id']!r}; expected canonical {canonical!r}"
    )


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_marks_failed_resource_with_unmounted_at(
    mock_fetch_one, mock_fetch_all, sandbox_client
):
    """The state='failed' INSERT must populate unmounted_at to satisfy the
    session_resources_terminal_has_unmounted_at CHECK constraint
    (migration 0004). Without unmounted_at the INSERT would CheckViolation."""
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())
    sid = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_file_row(file_id=file_id),
        _make_session_row(session_id=sid, agent_id=agent_id, environment_id=env_id),
        _make_resource_row(
            session_id=sid,
            mount_path="/mnt/missing.csv",
            config={"file_id": file_id},
            state="failed",
        ),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": file_id, "mount_path": "/mnt/missing.csv"},
        ],
    }
    with patch("app.routes.sessions.os.path.isfile", return_value=False):
        resp = client.post("/v1/sessions", json=payload, headers=AUTH)

    assert resp.status_code == 201, resp.text
    # The session_resources INSERT must include unmounted_at as the 7th positional.
    insert_call = next(
        c for c in mock_fetch_one.call_args_list
        if c.args and "INSERT INTO session_resources" in c.args[0]
    )
    sql = insert_call.args[0]
    assert "unmounted_at" in sql, "INSERT must include unmounted_at column"
    state = insert_call.args[5]
    unmounted_at = insert_call.args[7]
    assert state == "failed"
    assert unmounted_at is not None, "failed-state INSERT must supply unmounted_at"


@patch("app.routes.sessions.execute", new_callable=AsyncMock)
@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_destroys_container_on_db_failure(
    mock_fetch_one, mock_fetch_all, _mock_append, mock_execute, sandbox_client
):
    """If any DB write fails after sandbox.create succeeds, the orphaned
    container must be destroyed so it doesn't linger forever."""
    client, mock_sandbox = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    file_id = str(uuid.uuid4())

    # Validation succeeds; then INSERT INTO sessions raises (e.g. transient
    # connection failure or a constraint we forgot about).
    class FakeDBError(Exception):
        pass

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_file_row(file_id=file_id),
        FakeDBError("connection reset during sessions INSERT"),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "resources": [
            {"type": "file", "file_id": file_id, "mount_path": "/mnt/d"},
        ],
    }
    # Use a fresh TestClient that doesn't reraise server exceptions, so we
    # get the 500 response back instead of pytest exploding on the inner
    # FakeDBError. The crucial assertion is the cleanup, not the status code.
    from fastapi.testclient import TestClient
    from app.main import app
    quiet = TestClient(app, raise_server_exceptions=False)
    quiet.headers.update(AUTH)
    resp = quiet.post("/v1/sessions", json=payload)

    assert resp.status_code == 500
    mock_sandbox.create.assert_called_once()
    mock_sandbox.destroy.assert_called_once_with("container-abc")
