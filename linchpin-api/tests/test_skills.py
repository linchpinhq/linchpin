"""Tests for the Skills API (v0.4.0 PR1).

Covers:
- ``parse_skill_metadata`` frontmatter validation matrix.
- ``validate_skill_name`` slug rules.
- Bundle archive sniffing (tar.gz, zip, neither).
- Route surface: upload, list, get, delete, plus the
  ``re-upload same name`` update path.
"""

from __future__ import annotations

import io
import os
import tarfile
import uuid
import zipfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models import SKILL_BUNDLE_MAX_BYTES, validate_skill_name
from app.skills import (
    SkillBundleError,
    parse_skill_metadata,
    storage_path_for,
)


AUTH = {"Authorization": "Bearer test-secret-key"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tar_bundle(skill_md: str, extras: dict[str, bytes] | None = None) -> bytes:
    """Build a minimal tar.gz bundle in memory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        md_bytes = skill_md.encode()
        info = tarfile.TarInfo(name="SKILL.md")
        info.size = len(md_bytes)
        tf.addfile(info, io.BytesIO(md_bytes))
        for name, content in (extras or {}).items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_zip_bundle(skill_md: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("SKILL.md", skill_md)
    return buf.getvalue()


def _make_skill_row(*, name: str = "gh-pr", archived: bool = False):
    return {
        "id": uuid.uuid4(),
        "name": name,
        "description": "Creates pull requests. Use when the user asks to open a PR.",
        "workspace_id": uuid.UUID("00000000-0000-0000-0000-000000000000"),
        "bundle_sha256": "a" * 64,
        "bundle_size": 1024,
        "storage_path": "aa/aa/" + "a" * 64 + ".tar.gz",
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "archived_at": datetime(2025, 1, 1, tzinfo=timezone.utc) if archived else None,
    }


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """TestClient with mocked startup + skills root pointed at tmp_path."""
    monkeypatch.setenv("LINCHPIN_SKILLS_ROOT", str(tmp_path / "skills"))
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
# Slug + frontmatter validation (pure)
# ---------------------------------------------------------------------------


class TestValidateSkillName:
    @pytest.mark.parametrize("good", ["gh-pr", "xlsx", "pdf-wrangler", "a", "x1"])
    def test_accepts(self, good):
        assert validate_skill_name(good) == good

    @pytest.mark.parametrize("bad", [
        "",
        "Anthropic",                # reserved
        "claude-helper",            # reserved
        "linchpin-thing",           # reserved
        "-leading-hyphen",
        "trailing-hyphen-",
        "double--hyphen",
        "UpperCase",
        "has spaces",
        "has_underscore",
        "x" * 65,                   # too long
    ])
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            validate_skill_name(bad)


class TestParseSkillMetadata:
    def test_happy_path_tar(self):
        body = (
            "---\nname: gh-pr\n"
            "description: Creates PRs in GitHub.\n---\n\nSkill body.\n"
        )
        bundle = _make_tar_bundle(body)
        name, desc = parse_skill_metadata(bundle)
        assert name == "gh-pr"
        assert desc == "Creates PRs in GitHub."

    def test_happy_path_zip(self):
        body = "---\nname: xlsx\ndescription: Reads xlsx files.\n---\n"
        bundle = _make_zip_bundle(body)
        name, _ = parse_skill_metadata(bundle)
        assert name == "xlsx"

    def test_unknown_archive_format_rejected(self):
        with pytest.raises(SkillBundleError, match="not a recognized archive"):
            parse_skill_metadata(b"plain text, no magic")

    def test_missing_skill_md_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="OTHER.md")
            info.size = 4
            tf.addfile(info, io.BytesIO(b"data"))
        with pytest.raises(SkillBundleError, match="SKILL.md"):
            parse_skill_metadata(buf.getvalue())

    def test_missing_frontmatter_fence_rejected(self):
        bundle = _make_tar_bundle("# gh-pr\n\nNo frontmatter.")
        with pytest.raises(SkillBundleError, match="frontmatter fence"):
            parse_skill_metadata(bundle)

    def test_unclosed_frontmatter_rejected(self):
        bundle = _make_tar_bundle("---\nname: gh-pr\nno closing fence\nbody")
        with pytest.raises(SkillBundleError, match="closing"):
            parse_skill_metadata(bundle)

    def test_missing_name_rejected(self):
        bundle = _make_tar_bundle("---\ndescription: Just a desc.\n---\n")
        with pytest.raises(SkillBundleError, match="name"):
            parse_skill_metadata(bundle)

    def test_missing_description_rejected(self):
        bundle = _make_tar_bundle("---\nname: gh-pr\n---\n")
        with pytest.raises(SkillBundleError, match="description"):
            parse_skill_metadata(bundle)

    def test_reserved_name_rejected(self):
        bundle = _make_tar_bundle(
            "---\nname: claude-helper\ndescription: x\n---\n"
        )
        with pytest.raises(SkillBundleError, match="invalid skill name"):
            parse_skill_metadata(bundle)

    def test_description_too_long_rejected(self):
        bundle = _make_tar_bundle(
            f"---\nname: gh-pr\ndescription: {'x' * 2000}\n---\n"
        )
        with pytest.raises(SkillBundleError, match="≤ 1024"):
            parse_skill_metadata(bundle)


# ---------------------------------------------------------------------------
# storage_path_for
# ---------------------------------------------------------------------------


def test_storage_path_is_content_addressed():
    sha = "abcd1234" + "0" * 56
    assert storage_path_for(sha) == "ab/cd/" + sha + ".tar.gz"


# ---------------------------------------------------------------------------
# Route surface (mocked DB)
# ---------------------------------------------------------------------------


@patch("app.routes.skills.fetch_one", new_callable=AsyncMock)
def test_upload_skill_creates_row(mock_fetch_one, app_client, tmp_path):
    bundle = _make_tar_bundle(
        "---\nname: gh-pr\ndescription: Creates PRs.\n---\n# Body\n"
    )

    # First fetch_one: existing-row lookup (None means INSERT path).
    # Second fetch_one: INSERT RETURNING row.
    skill_row = _make_skill_row(name="gh-pr")
    mock_fetch_one.side_effect = [None, skill_row]

    resp = app_client.post(
        "/v1/skills",
        files={"bundle": ("skill.tar.gz", bundle, "application/gzip")},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "gh-pr"
    assert body["description"] == skill_row["description"]
    # Bundle bytes landed on disk under the content-addressable path.
    skills_root = tmp_path / "skills"
    # The path is content-addressed; we don't pin the sha but the
    # bundle was actually written.
    all_paths = list(skills_root.rglob("*.tar.gz"))
    assert len(all_paths) == 1
    assert all_paths[0].read_bytes() == bundle


@patch("app.routes.skills.fetch_one", new_callable=AsyncMock)
def test_upload_skill_invalid_frontmatter_returns_422(mock_fetch_one, app_client):
    bundle = _make_tar_bundle("# no frontmatter\n")
    resp = app_client.post(
        "/v1/skills",
        files={"bundle": ("skill.tar.gz", bundle, "application/gzip")},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_skill_bundle"
    mock_fetch_one.assert_not_called()


def test_upload_skill_too_large_returns_413(app_client):
    # Even fake bytes — the route checks size before parsing.
    huge = b"x" * (SKILL_BUNDLE_MAX_BYTES + 1)
    resp = app_client.post(
        "/v1/skills",
        files={"bundle": ("skill.tar.gz", huge, "application/gzip")},
        headers=AUTH,
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["error"] == "skill_too_large"


@patch("app.routes.skills.fetch_one", new_callable=AsyncMock)
def test_upload_skill_same_name_updates_row(mock_fetch_one, app_client):
    bundle = _make_tar_bundle(
        "---\nname: gh-pr\ndescription: Updated description.\n---\n"
    )
    existing_row = _make_skill_row(name="gh-pr")
    updated_row = _make_skill_row(name="gh-pr")
    updated_row["description"] = "Updated description."
    updated_row["id"] = existing_row["id"]
    mock_fetch_one.side_effect = [existing_row, updated_row]

    resp = app_client.post(
        "/v1/skills",
        files={"bundle": ("skill.tar.gz", bundle, "application/gzip")},
        headers=AUTH,
    )
    assert resp.status_code == 201
    assert resp.json()["description"] == "Updated description."
    # Two fetch_one calls: lookup + UPDATE RETURNING.
    assert mock_fetch_one.await_count == 2


@patch("app.routes.skills.fetch_all", new_callable=AsyncMock)
def test_list_skills_excludes_archived_by_default(mock_fetch_all, app_client):
    mock_fetch_all.return_value = [_make_skill_row(name="alpha")]
    resp = app_client.get("/v1/skills", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["data"][0]["name"] == "alpha"
    # SQL filters archived_at IS NULL.
    sql = mock_fetch_all.await_args.args[0]
    assert "archived_at IS NULL" in sql


@patch("app.routes.skills.fetch_all", new_callable=AsyncMock)
def test_list_skills_include_archived(mock_fetch_all, app_client):
    mock_fetch_all.return_value = [
        _make_skill_row(name="alpha"),
        _make_skill_row(name="beta", archived=True),
    ]
    resp = app_client.get("/v1/skills?include_archived=true", headers=AUTH)
    assert resp.status_code == 200
    sql = mock_fetch_all.await_args.args[0]
    assert "archived_at IS NULL" not in sql


@patch("app.routes.skills.fetch_one", new_callable=AsyncMock)
def test_get_skill_not_found(mock_fetch_one, app_client):
    mock_fetch_one.return_value = None
    resp = app_client.get(f"/v1/skills/{uuid.uuid4()}", headers=AUTH)
    assert resp.status_code == 404


def test_get_skill_invalid_uuid_returns_404(app_client):
    resp = app_client.get("/v1/skills/not-a-uuid", headers=AUTH)
    assert resp.status_code == 404


@patch("app.routes.skills.fetch_one", new_callable=AsyncMock)
def test_get_skill_returns_row(mock_fetch_one, app_client):
    row = _make_skill_row(name="gh-pr")
    mock_fetch_one.return_value = row
    resp = app_client.get(f"/v1/skills/{row['id']}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["name"] == "gh-pr"


@patch("app.routes.skills.fetch_one", new_callable=AsyncMock)
def test_delete_skill_soft_deletes(mock_fetch_one, app_client):
    row = _make_skill_row(name="gh-pr")
    archived = dict(row)
    archived["archived_at"] = datetime(2026, 5, 16, tzinfo=timezone.utc)
    mock_fetch_one.side_effect = [row, archived]
    resp = app_client.delete(f"/v1/skills/{row['id']}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is not None


@patch("app.routes.skills.fetch_one", new_callable=AsyncMock)
def test_delete_already_archived_is_idempotent(mock_fetch_one, app_client):
    archived = _make_skill_row(name="gh-pr", archived=True)
    mock_fetch_one.return_value = archived
    resp = app_client.delete(f"/v1/skills/{archived['id']}", headers=AUTH)
    assert resp.status_code == 200
    # Only one fetch_one — no UPDATE RETURNING because already archived.
    assert mock_fetch_one.await_count == 1


@patch("app.routes.skills.fetch_one", new_callable=AsyncMock)
def test_delete_skill_not_found(mock_fetch_one, app_client):
    mock_fetch_one.return_value = None
    resp = app_client.delete(f"/v1/skills/{uuid.uuid4()}", headers=AUTH)
    assert resp.status_code == 404
