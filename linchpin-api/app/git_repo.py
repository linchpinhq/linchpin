"""Git repository resource helpers (v0.5.0).

Clone-on-session-create + per-environment cache. The cache key is a
sha256 of ``(url, branch)`` so two sessions in the same environment
that point at the same repo + branch share one on-disk clone — the
spec's "saves network on repeat use" property.

The cache root is a separate volume from Files / Memory / Skills so
operators can isolate large clones onto their own storage. Cache
entries are immutable per (env, key) — when the agent needs newer
bytes, the orchestrator updates the existing clone with ``git fetch``
+ ``git checkout`` instead of re-cloning.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("linchpin-api.git_repo")


_GIT_BIN = "git"
DEFAULT_TIMEOUT_SECONDS = 300  # 5 minutes — large repos legitimately take time


class GitCloneError(Exception):
    """Raised when a git operation fails irrecoverably."""


def git_cache_root() -> str:
    """Root directory for cached git clones, separate from other
    LINCHPIN_*_ROOT volumes so ops teams can isolate the (typically
    much larger) repo data on its own disk.
    """
    return os.environ.get(
        "LINCHPIN_GIT_CACHE_ROOT", "/var/lib/linchpin/git_cache"
    )


def cache_key(url: str, branch: str | None) -> str:
    """Stable hash of (url, branch) for cache layout. We canonicalize
    the URL — strip credentials embedded in https URLs, drop trailing
    ``.git`` — so logically-equivalent refs share an entry."""
    canon = _canonicalize_url(url) + "@" + (branch or "")
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _canonicalize_url(url: str) -> str:
    """Strip ``user:pass@`` from https URLs and drop a trailing
    ``.git`` so two callers writing the same logical reference share
    one cache entry."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url.rstrip("/").removesuffix(".git")
    netloc = parsed.hostname or parsed.netloc
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    cleaned = urlunparse(parsed._replace(netloc=netloc))
    return cleaned.rstrip("/").removesuffix(".git")


def cache_dir_for(env_id: str, url: str, branch: str | None) -> str:
    """Absolute cache directory for one (env, url, branch) triple."""
    key = cache_key(url, branch)
    return os.path.join(git_cache_root(), env_id, key)


def _inject_token(url: str, token: str | None) -> str:
    """Embed ``x-access-token:<token>@`` into the URL for HTTPS clones.

    Mirrors the GitHub pattern that most providers accept (GitHub +
    GitLab personal access tokens, GitHub Apps installation tokens).
    For SSH or non-HTTPS URLs we leave the URL alone — the operator
    is expected to mount their SSH key separately.
    """
    if not token:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.scheme not in ("https", "http"):
        return url
    if "@" in (parsed.netloc or ""):
        # URL already has credentials; trust the caller.
        return url
    new_netloc = f"x-access-token:{token}@{parsed.netloc}"
    return urlunparse(parsed._replace(netloc=new_netloc))


_GIT_REF_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


def _validate_branch(branch: str | None) -> None:
    """Reject branch names that contain shell metacharacters or path
    traversal. We pass branch verbatim to ``git checkout`` after a
    bounded subprocess invocation, so the surface is small — but a
    branch like ``;rm -rf /`` is a bug regardless."""
    if branch is None:
        return
    if not _GIT_REF_RE.match(branch):
        raise GitCloneError(
            f"invalid branch name {branch!r}: must match {_GIT_REF_RE.pattern}"
        )


async def _run_git(
    *args: str,
    cwd: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``git`` with bounded time + return (rc, stdout, stderr).

    No shell expansion — args are passed as a list. Inherits the
    parent env by default so the operator's ``GIT_SSH_COMMAND`` / TLS
    settings flow through.
    """
    merged_env = {**os.environ, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        _GIT_BIN, *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=merged_env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise GitCloneError(
            f"git {args[0] if args else ''} timed out after {timeout}s"
        ) from exc
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def clone_or_update(
    *,
    env_id: str,
    url: str,
    branch: str | None,
    shallow: bool,
    token: str | None,
) -> str:
    """Idempotent clone / update of a remote into the env-scoped cache.

    Two sessions in the same env pointing at the same (url, branch)
    share one on-disk clone. Returns the absolute cache directory the
    caller should bind-mount into the container.

    On cache miss: ``git clone`` (depth 1 if ``shallow``). On hit:
    ``git fetch`` + ``git checkout`` to refresh.
    """
    _validate_branch(branch)
    target = cache_dir_for(env_id, url, branch)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    auth_url = _inject_token(url, token)

    if os.path.isdir(os.path.join(target, ".git")):
        # Cache hit — refresh.
        rc, _out, err = await _run_git(
            "fetch", "--prune", "origin",
            *(["--depth", "1"] if shallow else []),
            cwd=target,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        if rc != 0:
            raise GitCloneError(f"git fetch failed: {err.strip()}")
        if branch is not None:
            rc, _out, err = await _run_git(
                "checkout", branch, cwd=target,
                env={"GIT_TERMINAL_PROMPT": "0"},
            )
            if rc != 0:
                raise GitCloneError(
                    f"git checkout {branch} failed: {err.strip()}"
                )
            # Reset to remote head so a previous detached state doesn't
            # linger between sessions.
            rc, _out, err = await _run_git(
                "reset", "--hard", f"origin/{branch}", cwd=target,
                env={"GIT_TERMINAL_PROMPT": "0"},
            )
            if rc != 0:
                raise GitCloneError(
                    f"git reset --hard origin/{branch} failed: {err.strip()}"
                )
        return target

    # Cache miss — full clone (or shallow). Land into a tmp dir then
    # rename to the final path so a concurrent reader never sees a
    # half-cloned tree.
    tmp = target + ".tmp"
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    clone_args = ["clone"]
    if shallow:
        clone_args += ["--depth", "1"]
    if branch:
        clone_args += ["--branch", branch]
    clone_args += [auth_url, tmp]
    rc, _out, err = await _run_git(
        *clone_args, env={"GIT_TERMINAL_PROMPT": "0"},
    )
    if rc != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        # Redact the injected token from the error message before
        # propagating — accidental log leakage of an embedded credential
        # is the primary attack we need to dodge here.
        sanitized = err.replace(token, "[redacted]") if token else err
        raise GitCloneError(f"git clone failed: {sanitized.strip()}")
    # If a token was embedded in the URL, rewrite the remote to the
    # clean form so a subsequent fetch doesn't print the secret back.
    if token:
        rc, _out, err = await _run_git(
            "remote", "set-url", "origin", url, cwd=tmp,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        if rc != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise GitCloneError(f"git remote set-url failed: {err.strip()}")
    os.replace(tmp, target)
    return target
