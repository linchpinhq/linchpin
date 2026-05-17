"""Skill bundle helpers (v0.4.0).

Skill bundles are tar.gz archives containing ``SKILL.md`` + bundled
scripts/resources. We parse the frontmatter at upload time to extract
``name`` + ``description`` (level-1 progressive disclosure metadata) and
persist the raw archive on disk under ``LINCHPIN_SKILLS_ROOT`` at a
content-addressable path so identical bundles dedupe.

Materialization into the sandbox is handled in PR2 — this module owns
the upload / unpack / lookup primitives only.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tarfile
import zipfile

from app.models import (
    SKILL_DESCRIPTION_MAX_LEN,
    SKILL_NAME_MAX_LEN,
    validate_skill_name,
)

logger = logging.getLogger("linchpin-api.skills")


def skills_root() -> str:
    """Root directory for skill bundle storage.

    Separate from Files / Memory roots so operators can put skill
    bundles on their own volume (typically small + read-mostly).
    """
    return os.environ.get(
        "LINCHPIN_SKILLS_ROOT", "/var/lib/linchpin/skills"
    )


def storage_path_for(sha: str) -> str:
    """Relative content-addressable path for a bundle sha256."""
    return f"{sha[:2]}/{sha[2:4]}/{sha}.tar.gz"


def absolute_storage_path(sha: str) -> str:
    """Absolute on-disk path for a bundle sha256."""
    return os.path.join(skills_root(), storage_path_for(sha))


class SkillBundleError(Exception):
    """Raised when a skill bundle is malformed."""


def _parse_frontmatter(body: str) -> dict[str, str]:
    """Parse a tiny YAML-like frontmatter block from the head of SKILL.md.

    Accepts only ``key: value`` lines between leading ``---`` fences.
    Values are not unquoted/typed — the caller validates them.
    """
    if not body.startswith("---"):
        raise SkillBundleError(
            "SKILL.md must begin with a '---' frontmatter fence"
        )
    lines = body.split("\n")
    if len(lines) < 2:
        raise SkillBundleError("SKILL.md frontmatter is truncated")
    # Find the closing fence.
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise SkillBundleError(
            "SKILL.md frontmatter is missing its closing '---' fence"
        )
    fm: dict[str, str] = {}
    for line in lines[1:end]:
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise SkillBundleError(
                f"SKILL.md frontmatter line missing ':' separator: {line!r}"
            )
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip().strip('"').strip("'")
    return fm


def _read_skill_md_from_tar(data: bytes) -> str:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for member in tf:
                # Strip leading dir if all entries share one (common in
                # tar archives made from a directory).
                name = member.name
                if name.endswith("/SKILL.md") or name == "SKILL.md":
                    f = tf.extractfile(member)
                    if f is None:
                        raise SkillBundleError(
                            "SKILL.md is not a regular file in the bundle"
                        )
                    return f.read().decode("utf-8", errors="replace")
    except tarfile.TarError as exc:
        raise SkillBundleError(f"bundle is not a valid tar.gz: {exc}") from exc
    raise SkillBundleError("bundle does not contain SKILL.md at its root")


def _read_skill_md_from_zip(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data), mode="r") as zf:
            for name in zf.namelist():
                if name.endswith("/SKILL.md") or name == "SKILL.md":
                    return zf.read(name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        raise SkillBundleError(f"bundle is not a valid zip: {exc}") from exc
    raise SkillBundleError("bundle does not contain SKILL.md at its root")


_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK\x03\x04"


def read_skill_md(bundle_bytes: bytes) -> str:
    """Sniff the archive format and extract SKILL.md as a UTF-8 string.

    Accepts gzip-compressed tar (``.tar.gz``) and zip (``.zip``) — the two
    formats ``linchpin skill build`` is likely to produce. We sniff by
    magic bytes rather than trusting the filename so a misnamed upload
    still works.
    """
    if bundle_bytes.startswith(_GZIP_MAGIC):
        return _read_skill_md_from_tar(bundle_bytes)
    if bundle_bytes.startswith(_ZIP_MAGIC):
        return _read_skill_md_from_zip(bundle_bytes)
    raise SkillBundleError(
        "bundle is not a recognized archive (expected .tar.gz or .zip)"
    )


def parse_skill_metadata(bundle_bytes: bytes) -> tuple[str, str]:
    """Extract + validate ``(name, description)`` from a skill bundle.

    The frontmatter validators mirror the CLI's so upload + build paths
    can't drift on what counts as valid.
    """
    skill_md = read_skill_md(bundle_bytes)
    fm = _parse_frontmatter(skill_md)
    name = fm.get("name")
    description = fm.get("description")
    if not name:
        raise SkillBundleError("SKILL.md frontmatter is missing 'name'")
    if not description:
        raise SkillBundleError(
            "SKILL.md frontmatter is missing 'description'"
        )
    try:
        validate_skill_name(name)
    except ValueError as exc:
        raise SkillBundleError(f"invalid skill name in frontmatter: {exc}") from exc
    if len(description) > SKILL_DESCRIPTION_MAX_LEN:
        raise SkillBundleError(
            f"skill description must be ≤ {SKILL_DESCRIPTION_MAX_LEN} chars "
            f"(got {len(description)})"
        )
    if len(name) > SKILL_NAME_MAX_LEN:
        # Already enforced by validate_skill_name but guard for completeness.
        raise SkillBundleError(
            f"skill name must be ≤ {SKILL_NAME_MAX_LEN} chars"
        )
    return name, description
