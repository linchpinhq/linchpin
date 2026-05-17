"""Tests for ``app.git_repo`` (v0.5.0 PR3).

Pin: cache key stability, URL canonicalization, branch-name
validation, token injection into HTTPS URLs, ``clone_or_update``
calling ``git`` with the right argv on miss + hit, error path
sanitizing tokens out of stderr.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.git_repo import (
    GitCloneError,
    _canonicalize_url,
    _inject_token,
    cache_dir_for,
    cache_key,
    clone_or_update,
    git_cache_root,
)


# ---------------------------------------------------------------------------
# Cache key + canonicalization
# ---------------------------------------------------------------------------


class TestCacheKey:
    def test_stable_for_same_inputs(self):
        a = cache_key("https://github.com/x/y", "main")
        b = cache_key("https://github.com/x/y", "main")
        assert a == b

    def test_branch_changes_key(self):
        a = cache_key("https://github.com/x/y", "main")
        b = cache_key("https://github.com/x/y", "develop")
        assert a != b

    def test_trailing_slash_and_dotgit_ignored(self):
        a = cache_key("https://github.com/x/y", "main")
        b = cache_key("https://github.com/x/y/", "main")
        c = cache_key("https://github.com/x/y.git", "main")
        assert a == b == c

    def test_credentials_in_url_ignored(self):
        """A token embedded in the URL must not change the cache key —
        otherwise rotation would force a re-clone every time."""
        clean = cache_key("https://github.com/x/y", "main")
        with_creds = cache_key("https://user:tok@github.com/x/y", "main")
        assert clean == with_creds


def test_cache_dir_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("LINCHPIN_GIT_CACHE_ROOT", str(tmp_path))
    env_id = "00000000-0000-0000-0000-000000000001"
    expected_key = hashlib.sha256(
        b"https://github.com/x/y@main",
    ).hexdigest()
    assert cache_dir_for(env_id, "https://github.com/x/y", "main") == str(
        tmp_path / env_id / expected_key
    )


def test_canonicalize_strips_credentials_and_dotgit():
    assert _canonicalize_url("https://u:p@github.com/x/y.git/") == \
        "https://github.com/x/y"


# ---------------------------------------------------------------------------
# Token injection
# ---------------------------------------------------------------------------


class TestInjectToken:
    def test_none_token_pass_through(self):
        assert _inject_token("https://x/y", None) == "https://x/y"

    def test_https_embeds_token(self):
        assert _inject_token("https://github.com/x/y", "ghp_secret") == (
            "https://x-access-token:ghp_secret@github.com/x/y"
        )

    def test_url_with_existing_creds_left_alone(self):
        url = "https://user:pw@x/y"
        assert _inject_token(url, "ghp_other") == url

    def test_non_http_left_alone(self):
        assert _inject_token("git@github.com:x/y.git", "tok") == \
            "git@github.com:x/y.git"


# ---------------------------------------------------------------------------
# clone_or_update — happy paths via mocked subprocess
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_run_git():
    """Patch ``_run_git`` so tests don't shell out to a real git.

    Side-effect: when the mocked call is a clone, simulate git's
    behavior by creating the target directory + a ``.git`` subdir so
    the caller's subsequent ``os.replace`` + cache-hit detection both
    work.
    """
    calls: list[tuple[tuple, dict]] = []

    async def fake(*args, **kwargs):
        calls.append((args, kwargs))
        if args and args[0] == "clone":
            # Final arg is the target dir for ``git clone``.
            target = args[-1]
            os.makedirs(os.path.join(target, ".git"), exist_ok=True)
        return (0, "", "")

    with patch("app.git_repo._run_git", side_effect=fake) as m:
        m.calls = calls
        yield m


@pytest.mark.asyncio
async def test_clone_miss_runs_git_clone(monkeypatch, tmp_path, mock_run_git):
    monkeypatch.setenv("LINCHPIN_GIT_CACHE_ROOT", str(tmp_path))
    target = await clone_or_update(
        env_id="env-1",
        url="https://github.com/x/y",
        branch="main",
        shallow=True,
        token=None,
    )
    # The target ends in the expected cache key path
    expected_dir = cache_dir_for("env-1", "https://github.com/x/y", "main")
    assert target == expected_dir
    # First call was a clone with --depth 1 + --branch main
    args = mock_run_git.calls[0][0]
    assert args[0] == "clone"
    assert "--depth" in args and "1" in args
    assert "--branch" in args and "main" in args
    # The URL was passed without a token prefix (token was None).
    assert any(a == "https://github.com/x/y" for a in args)


@pytest.mark.asyncio
async def test_clone_with_token_strips_from_remote_after_clone(
    monkeypatch, tmp_path, mock_run_git,
):
    monkeypatch.setenv("LINCHPIN_GIT_CACHE_ROOT", str(tmp_path))
    await clone_or_update(
        env_id="env-1",
        url="https://github.com/x/y",
        branch="main",
        shallow=True,
        token="ghp_secret",
    )
    # First call: clone with token-bearing URL.
    clone_args = mock_run_git.calls[0][0]
    assert any(
        "x-access-token:ghp_secret" in str(a) for a in clone_args
    )
    # Second call: remote set-url back to clean URL so fetch doesn't
    # leak the token in logs.
    set_url_args = mock_run_git.calls[1][0]
    assert set_url_args[:3] == ("remote", "set-url", "origin")
    assert set_url_args[3] == "https://github.com/x/y"


@pytest.mark.asyncio
async def test_clone_hit_runs_fetch_and_reset(monkeypatch, tmp_path, mock_run_git):
    """When the cache dir already has a ``.git`` subdir, we fetch
    instead of cloning — and the branch is reset to origin/<branch>."""
    monkeypatch.setenv("LINCHPIN_GIT_CACHE_ROOT", str(tmp_path))
    # Pre-create the target with a fake .git/ so cache-hit branch fires.
    target_dir = Path(cache_dir_for("env-1", "https://github.com/x/y", "main"))
    (target_dir / ".git").mkdir(parents=True)

    await clone_or_update(
        env_id="env-1",
        url="https://github.com/x/y",
        branch="main",
        shallow=True,
        token=None,
    )
    # fetch + checkout + reset --hard origin/<branch>
    methods = [c[0][0] for c in mock_run_git.calls]
    assert methods == ["fetch", "checkout", "reset"]
    reset_args = mock_run_git.calls[-1][0]
    assert reset_args == ("reset", "--hard", "origin/main")


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_failure_redacts_token_in_error(monkeypatch, tmp_path):
    """A failing clone whose stderr contains the embedded token must
    not propagate the secret in the exception message."""
    monkeypatch.setenv("LINCHPIN_GIT_CACHE_ROOT", str(tmp_path))

    async def fake_run(*args, **kwargs):
        return (
            128,
            "",
            "fatal: Authentication failed for 'https://x-access-token:ghp_secret@github.com/x/y/'",
        )

    with patch("app.git_repo._run_git", side_effect=fake_run):
        with pytest.raises(GitCloneError) as exc_info:
            await clone_or_update(
                env_id="env-1",
                url="https://github.com/x/y",
                branch=None,
                shallow=True,
                token="ghp_secret",
            )
    assert "ghp_secret" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)


@pytest.mark.asyncio
async def test_invalid_branch_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("LINCHPIN_GIT_CACHE_ROOT", str(tmp_path))
    with pytest.raises(GitCloneError, match="invalid branch"):
        await clone_or_update(
            env_id="env-1",
            url="https://github.com/x/y",
            branch="evil;rm -rf",
            shallow=True,
            token=None,
        )


# ---------------------------------------------------------------------------
# git_cache_root env override
# ---------------------------------------------------------------------------


def test_git_cache_root_env_override(monkeypatch):
    monkeypatch.setenv("LINCHPIN_GIT_CACHE_ROOT", "/srv/linchpin/git")
    assert git_cache_root() == "/srv/linchpin/git"
