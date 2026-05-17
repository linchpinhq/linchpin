"""Tests for v0.6.0 PR1 — Outcomes (rubric + agent grader).

Covers:
- ``OutcomeDefinition`` validation: definition + rubric size, weight
  sum, grader discriminator.
- ``CreateSessionRequest`` accepts ``outcome``; grader existence is
  checked in the route.
- POST/GET ``/outcome_evaluations`` round-trip, event emission, and
  auto-termination when the score crosses the success threshold.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.models import (
    OutcomeAgentGrader,
    OutcomeDefinition,
    RubricCriterion,
)


AUTH = {"Authorization": "Bearer test-secret-key"}


# ---------------------------------------------------------------------------
# Model-level validation
# ---------------------------------------------------------------------------


class TestRubricCriterion:
    def test_happy_path(self):
        c = RubricCriterion(criterion="tests pass", weight=0.5)
        assert c.weight == 0.5

    def test_empty_criterion_rejected(self):
        with pytest.raises(ValidationError):
            RubricCriterion(criterion="", weight=0.5)

    def test_weight_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            RubricCriterion(criterion="x", weight=1.5)
        with pytest.raises(ValidationError):
            RubricCriterion(criterion="x", weight=-0.1)


class TestOutcomeDefinition:
    _GRADER = {"type": "agent", "agent_id": str(uuid.uuid4())}

    def test_happy_path(self):
        o = OutcomeDefinition.model_validate({
            "definition": "All tests pass and a PR exists.",
            "rubric": [
                {"criterion": "tests pass", "weight": 0.6},
                {"criterion": "PR open", "weight": 0.4},
            ],
            "grader": self._GRADER,
        })
        assert isinstance(o.grader, OutcomeAgentGrader)
        assert o.success_threshold == 0.8
        assert o.auto_terminate is True

    def test_empty_rubric_rejected(self):
        with pytest.raises(ValidationError, match="at least one criterion"):
            OutcomeDefinition.model_validate({
                "definition": "x",
                "rubric": [],
                "grader": self._GRADER,
            })

    def test_rubric_too_large_rejected(self):
        with pytest.raises(ValidationError, match="16-criterion cap"):
            OutcomeDefinition.model_validate({
                "definition": "x",
                "rubric": [
                    {"criterion": f"c{i}", "weight": 1.0 / 17}
                    for i in range(17)
                ],
                "grader": self._GRADER,
            })

    def test_weights_must_sum_to_one(self):
        with pytest.raises(ValidationError, match="sum to 1.0"):
            OutcomeDefinition.model_validate({
                "definition": "x",
                "rubric": [
                    {"criterion": "a", "weight": 0.3},
                    {"criterion": "b", "weight": 0.3},
                ],
                "grader": self._GRADER,
            })

    def test_weights_close_to_one_within_tolerance(self):
        # 0.5 + 0.3 + 0.2 — binary representation drifts slightly
        o = OutcomeDefinition.model_validate({
            "definition": "x",
            "rubric": [
                {"criterion": "a", "weight": 0.5},
                {"criterion": "b", "weight": 0.3},
                {"criterion": "c", "weight": 0.2},
            ],
            "grader": self._GRADER,
        })
        assert len(o.rubric) == 3

    def test_unknown_grader_type_rejected(self):
        with pytest.raises(ValidationError):
            OutcomeDefinition.model_validate({
                "definition": "x",
                "rubric": [{"criterion": "a", "weight": 1.0}],
                "grader": {"type": "deterministic", "rules": []},
            })

    def test_definition_too_long_rejected(self):
        with pytest.raises(ValidationError):
            OutcomeDefinition.model_validate({
                "definition": "x" * 5000,
                "rubric": [{"criterion": "a", "weight": 1.0}],
                "grader": self._GRADER,
            })

    def test_custom_success_threshold(self):
        o = OutcomeDefinition.model_validate({
            "definition": "x",
            "rubric": [{"criterion": "a", "weight": 1.0}],
            "grader": self._GRADER,
            "success_threshold": 0.95,
            "auto_terminate": False,
        })
        assert o.success_threshold == 0.95
        assert o.auto_terminate is False


# ---------------------------------------------------------------------------
# Route surface — uses the existing sandbox_client fixture pattern
# ---------------------------------------------------------------------------


def _make_agent_row(*, agent_id: str | None = None, archived: bool = False):
    return {
        "id": uuid.UUID(agent_id) if agent_id else uuid.uuid4(),
        "name": "test-agent",
        "version": 1,
        "model": {"provider": "openrouter", "id": "anthropic/claude-sonnet-4", "base_url": None},
        "system": "You are helpful.",
        "tools": [],
        "mcp_servers": [],
        "skills": [],
        "description": None,
        "metadata": {},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "archived_at": datetime(2025, 1, 1, tzinfo=timezone.utc) if archived else None,
    }


def _make_env_row(*, env_id: str | None = None):
    return {
        "id": uuid.UUID(env_id) if env_id else uuid.uuid4(),
        "name": "test-env",
        "config": {"networking": {"type": "none"}},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


def _make_session_row(*, session_id=None, agent_id=None, environment_id=None, outcome=None, status="running"):
    return {
        "id": uuid.UUID(session_id) if session_id else uuid.uuid4(),
        "agent_id": uuid.UUID(agent_id) if agent_id else uuid.uuid4(),
        "agent_version": 1,
        "environment_id": uuid.UUID(environment_id) if environment_id else uuid.uuid4(),
        "status": status,
        "container_id": "container-abc",
        "title": None,
        "metadata": {},
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "archived_at": None,
        "last_event_cursor": None,
        "ttl_seconds": None,
        "stats": {"total_events": 0, "tool_calls": 0, "model_turns": 0},
        "usage": {
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        },
        "vault_ids": [],
        "linchpin_api_version": None,
        "outcome": outcome,
        "parent_session_id": None,
    }


@pytest.fixture()
def sandbox_client(tmp_path):
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
        patch("app.routes.sessions.watch_memory_store", new_callable=AsyncMock),
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


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_with_outcome_persists(mock_fetch_one, mock_fetch_all, sandbox_client):
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    grader_id = str(uuid.uuid4())
    sid = str(uuid.uuid4())

    outcome = {
        "definition": "Tests pass and a PR is open",
        "rubric": [
            {"criterion": "tests pass", "weight": 0.7},
            {"criterion": "PR exists", "weight": 0.3},
        ],
        "grader": {"type": "agent", "agent_id": grader_id},
    }

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_agent_row(agent_id=grader_id),  # grader existence check
        _make_session_row(
            session_id=sid, agent_id=agent_id, environment_id=env_id,
            outcome=outcome,
        ),
    ]
    mock_fetch_all.return_value = []

    payload = {
        "agent_id": agent_id,
        "environment_id": env_id,
        "outcome": outcome,
    }
    resp = client.post("/v1/sessions", json=payload, headers=AUTH)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["outcome"]["definition"] == "Tests pass and a PR is open"
    assert body["outcome"]["grader"]["agent_id"] == grader_id


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_outcome_grader_not_found_returns_422(
    mock_fetch_one, mock_fetch_all, sandbox_client,
):
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    grader_id = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        None,  # grader missing
    ]
    mock_fetch_all.return_value = []

    outcome = {
        "definition": "x",
        "rubric": [{"criterion": "a", "weight": 1.0}],
        "grader": {"type": "agent", "agent_id": grader_id},
    }
    resp = client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "environment_id": env_id, "outcome": outcome},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "grader" in resp.json()["detail"]["message"]


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_create_session_outcome_grader_archived_returns_422(
    mock_fetch_one, mock_fetch_all, sandbox_client,
):
    client, _ = sandbox_client
    agent_id = str(uuid.uuid4())
    env_id = str(uuid.uuid4())
    grader_id = str(uuid.uuid4())

    mock_fetch_one.side_effect = [
        _make_agent_row(agent_id=agent_id),
        _make_env_row(env_id=env_id),
        _make_agent_row(agent_id=grader_id, archived=True),
    ]
    mock_fetch_all.return_value = []

    outcome = {
        "definition": "x",
        "rubric": [{"criterion": "a", "weight": 1.0}],
        "grader": {"type": "agent", "agent_id": grader_id},
    }
    resp = client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "environment_id": env_id, "outcome": outcome},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "archived" in resp.json()["detail"]["message"]


@patch("app.routes.sessions.execute", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
def test_post_outcome_evaluation_below_threshold_no_auto_terminate(
    mock_append, mock_fetch_one, mock_fetch_all, mock_execute, sandbox_client,
):
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    eval_id = uuid.uuid4()
    outcome = {
        "definition": "x",
        "rubric": [{"criterion": "a", "weight": 1.0}],
        "grader": {"type": "agent", "agent_id": str(uuid.uuid4())},
        "success_threshold": 0.8,
        "auto_terminate": True,
    }
    sess_row = _make_session_row(session_id=sid, outcome=json.dumps(outcome))
    mock_fetch_one.side_effect = [
        sess_row,
        {
            "id": eval_id,
            "session_id": uuid.UUID(sid),
            "grader_id": None,
            "score": 0.5,
            "rationale": "missed criterion a",
            "criteria": [],
            "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        },
    ]
    resp = client.post(
        f"/v1/sessions/{sid}/outcome_evaluations",
        json={"score": 0.5, "rationale": "missed criterion a"},
        headers=AUTH,
    )
    assert resp.status_code == 201
    assert resp.json()["score"] == 0.5
    # Event emitted, but no UPDATE to terminate.
    types = [c.args[1] for c in mock_append.await_args_list]
    assert "session.outcome_evaluation_ended" in types
    assert "session.status_terminated" not in types
    mock_execute.assert_not_called()


@patch("app.routes.sessions.execute", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
def test_post_outcome_evaluation_above_threshold_auto_terminates(
    mock_append, mock_fetch_one, mock_fetch_all, mock_execute, sandbox_client,
):
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    eval_id = uuid.uuid4()
    outcome = {
        "definition": "x",
        "rubric": [{"criterion": "a", "weight": 1.0}],
        "grader": {"type": "agent", "agent_id": str(uuid.uuid4())},
        "success_threshold": 0.8,
        "auto_terminate": True,
    }
    sess_row = _make_session_row(session_id=sid, outcome=json.dumps(outcome))
    mock_fetch_one.side_effect = [
        sess_row,
        {
            "id": eval_id,
            "session_id": uuid.UUID(sid),
            "grader_id": None,
            "score": 0.95,
            "rationale": "all good",
            "criteria": [],
            "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        },
    ]
    resp = client.post(
        f"/v1/sessions/{sid}/outcome_evaluations",
        json={"score": 0.95, "rationale": "all good"},
        headers=AUTH,
    )
    assert resp.status_code == 201

    types = [c.args[1] for c in mock_append.await_args_list]
    assert "session.outcome_evaluation_ended" in types
    assert "session.status_terminated" in types
    # UPDATE issued to transition status.
    mock_execute.assert_awaited()
    update_sql = mock_execute.await_args.args[0]
    assert "status = 'terminated'" in update_sql


@patch("app.routes.sessions.execute", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
@patch("app.routes.sessions.append_event", new_callable=AsyncMock)
def test_post_outcome_evaluation_auto_terminate_false(
    mock_append, mock_fetch_one, mock_fetch_all, mock_execute, sandbox_client,
):
    """Even with a passing score, ``auto_terminate: false`` keeps the
    session running so the caller can decide when to wrap up."""
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    eval_id = uuid.uuid4()
    outcome = {
        "definition": "x",
        "rubric": [{"criterion": "a", "weight": 1.0}],
        "grader": {"type": "agent", "agent_id": str(uuid.uuid4())},
        "success_threshold": 0.5,
        "auto_terminate": False,
    }
    sess_row = _make_session_row(session_id=sid, outcome=json.dumps(outcome))
    mock_fetch_one.side_effect = [
        sess_row,
        {
            "id": eval_id,
            "session_id": uuid.UUID(sid),
            "grader_id": None,
            "score": 1.0,
            "rationale": "perfect",
            "criteria": [],
            "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        },
    ]
    resp = client.post(
        f"/v1/sessions/{sid}/outcome_evaluations",
        json={"score": 1.0},
        headers=AUTH,
    )
    assert resp.status_code == 201
    types = [c.args[1] for c in mock_append.await_args_list]
    assert "session.status_terminated" not in types
    mock_execute.assert_not_called()


@patch("app.routes.sessions.fetch_all", new_callable=AsyncMock)
@patch("app.routes.sessions.fetch_one", new_callable=AsyncMock)
def test_list_outcome_evaluations_returns_history(
    mock_fetch_one, mock_fetch_all, sandbox_client,
):
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    mock_fetch_one.return_value = {"id": uuid.UUID(sid)}
    mock_fetch_all.return_value = [
        {
            "id": uuid.uuid4(),
            "session_id": uuid.UUID(sid),
            "grader_id": None,
            "score": 0.9,
            "rationale": "good",
            "criteria": [],
            "created_at": datetime(2025, 1, 2, tzinfo=timezone.utc),
        },
        {
            "id": uuid.uuid4(),
            "session_id": uuid.UUID(sid),
            "grader_id": None,
            "score": 0.4,
            "rationale": "not yet",
            "criteria": [],
            "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        },
    ]
    resp = client.get(f"/v1/sessions/{sid}/outcome_evaluations", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [d["score"] for d in data] == [0.9, 0.4]


def test_post_outcome_evaluation_score_out_of_range_rejected(sandbox_client):
    client, _ = sandbox_client
    sid = str(uuid.uuid4())
    resp = client.post(
        f"/v1/sessions/{sid}/outcome_evaluations",
        json={"score": 1.5},
        headers=AUTH,
    )
    assert resp.status_code == 422
