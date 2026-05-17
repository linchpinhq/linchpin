"""Tests for v0.4.0 PR2 — agent.skills[] field + sandbox materialization +
progressive-disclosure system prompt block.

The route-level integration tests reuse the ``sandbox_client`` fixture
from test_session_resources via direct import-by-path; the unit tests
exercise ``materialize_skill_bundle`` and
``render_skills_system_prompt_block`` directly.
"""

from __future__ import annotations

import io
import os
import tarfile
import uuid
import zipfile
from pathlib import Path

import pytest

from app.models import MAX_SKILLS_PER_AGENT, CreateAgentRequest
from app.skills import (
    materialize_skill_bundle,
    render_skills_system_prompt_block,
    session_skills_cache_dir,
    session_skills_root,
)


# ---------------------------------------------------------------------------
# CreateAgentRequest.skills validation
# ---------------------------------------------------------------------------


_BASE_REQ = {
    "name": "tester",
    "model": {"provider": "openrouter", "id": "claude", "base_url": None},
    "system": "you are helpful",
    "tools": [],
    "mcp_servers": [],
}


class TestAgentSkillsCap:
    def test_zero_skills_default(self):
        req = CreateAgentRequest.model_validate(_BASE_REQ)
        assert req.skills == []

    def test_cap_enforced(self):
        too_many = [str(uuid.uuid4()) for _ in range(MAX_SKILLS_PER_AGENT + 1)]
        with pytest.raises(Exception, match="exceeds 8-per-agent cap"):
            CreateAgentRequest.model_validate({**_BASE_REQ, "skills": too_many})

    def test_duplicate_rejected(self):
        sid = str(uuid.uuid4())
        with pytest.raises(Exception, match="duplicate"):
            CreateAgentRequest.model_validate({**_BASE_REQ, "skills": [sid, sid]})

    def test_eight_is_ok(self):
        ids = [str(uuid.uuid4()) for _ in range(MAX_SKILLS_PER_AGENT)]
        req = CreateAgentRequest.model_validate({**_BASE_REQ, "skills": ids})
        assert req.skills == ids


# ---------------------------------------------------------------------------
# materialize_skill_bundle
# ---------------------------------------------------------------------------


def _write_tar_bundle(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, mode="w:gz") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))


def _write_zip_bundle(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, mode="w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)


def test_materialize_tar_extracts_files(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    bundle = tmp_path / "skill.tar.gz"
    _write_tar_bundle(
        bundle,
        {
            "SKILL.md": b"---\nname: gh-pr\ndescription: x\n---\nbody",
            "scripts/run.sh": b"#!/bin/sh\necho hi\n",
        },
    )
    cache = materialize_skill_bundle(
        bundle_path=str(bundle), session_id="sess-1", skill_name="gh-pr",
    )
    cache_path = Path(cache)
    assert (cache_path / "SKILL.md").exists()
    assert (cache_path / "scripts" / "run.sh").exists()
    assert cache_path == Path(session_skills_cache_dir("sess-1", "gh-pr"))


def test_materialize_zip_extracts_files(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    bundle = tmp_path / "skill.zip"
    _write_zip_bundle(bundle, {"SKILL.md": b"x", "lib/util.py": b"# util"})
    cache = materialize_skill_bundle(
        bundle_path=str(bundle), session_id="sess-1", skill_name="xlsx",
    )
    cache_path = Path(cache)
    assert (cache_path / "SKILL.md").exists()
    assert (cache_path / "lib" / "util.py").exists()


def test_materialize_skips_traversal_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    bundle = tmp_path / "evil.tar.gz"
    _write_tar_bundle(
        bundle,
        {
            "SKILL.md": b"safe",
            "../escape.txt": b"would escape",
            "/etc/passwd": b"would also escape",
        },
    )
    cache = materialize_skill_bundle(
        bundle_path=str(bundle), session_id="sess-1", skill_name="x",
    )
    cache_path = Path(cache)
    assert (cache_path / "SKILL.md").exists()
    # Traversal entries dropped — no escape happened.
    assert not (cache_path.parent / "escape.txt").exists()
    assert not Path("/etc/passwd_test_should_not_exist").exists()


def test_session_skills_root_is_parent_of_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    root = session_skills_root("sess-1")
    dir_a = session_skills_cache_dir("sess-1", "alpha")
    dir_b = session_skills_cache_dir("sess-1", "beta")
    assert os.path.dirname(dir_a) == root
    assert os.path.dirname(dir_b) == root


# ---------------------------------------------------------------------------
# render_skills_system_prompt_block
# ---------------------------------------------------------------------------


def test_render_empty_returns_empty_string():
    assert render_skills_system_prompt_block([]) == ""


def test_render_includes_name_description_and_path():
    block = render_skills_system_prompt_block([
        {"name": "gh-pr", "description": "Creates PRs."},
        {"name": "xlsx", "description": "Reads spreadsheets."},
    ])
    assert "<linchpin:skills>" in block
    assert "</linchpin:skills>" in block
    assert "gh-pr: Creates PRs." in block
    assert "xlsx: Reads spreadsheets." in block
    assert "/mnt/skills/gh-pr/SKILL.md" in block
    assert "/mnt/skills/xlsx/SKILL.md" in block
