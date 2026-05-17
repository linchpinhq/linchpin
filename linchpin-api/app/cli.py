"""Linchpin command-line tool.

Entry point exposed via ``[project.scripts]`` in ``pyproject.toml``:

    linchpin skill build PATH [--output OUT]

The ``skill build`` subcommand packages a directory containing
``SKILL.md`` + scripts/resources into a ``.tar.gz`` ready to upload to
``POST /v1/skills``. Validates the frontmatter against the same rules
the API enforces so a bundle that builds cleanly is guaranteed to
upload cleanly.
"""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
from pathlib import Path

from app.skills import SkillBundleError, parse_skill_metadata


# Files we never want inside a skill bundle. Keep small + explicit;
# operators can override with --include-hidden if they really need to.
_DEFAULT_EXCLUDE_NAMES = frozenset({
    ".git", ".gitignore", "__pycache__", ".DS_Store", ".pytest_cache",
    ".mypy_cache", ".venv", "venv", "node_modules",
})


class CliError(Exception):
    """Raised for user-facing CLI failures."""


def _iter_bundle_files(root: Path, *, include_hidden: bool) -> list[Path]:
    """Walk *root* and yield every file we'll include in the tarball.

    Excludes ``_DEFAULT_EXCLUDE_NAMES`` outright and (when
    ``include_hidden`` is false) anything whose path component starts
    with a dot, so the bundle doesn't ship the contributor's editor
    state. Sorted output keeps the resulting tar bit-stable.
    """
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Filter dirs in-place so os.walk doesn't descend into them.
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _DEFAULT_EXCLUDE_NAMES
            and (include_hidden or not d.startswith("."))
        )
        for name in sorted(filenames):
            if name in _DEFAULT_EXCLUDE_NAMES:
                continue
            if not include_hidden and name.startswith("."):
                continue
            files.append(Path(dirpath) / name)
    return files


def _validate_input_dir(root: Path) -> bytes:
    """Read SKILL.md, parse + validate the frontmatter the same way the
    API will. Returns the raw bytes for diagnostic display only.
    """
    if not root.exists():
        raise CliError(f"path does not exist: {root}")
    if not root.is_dir():
        raise CliError(f"path is not a directory: {root}")
    skill_md = root / "SKILL.md"
    if not skill_md.exists():
        raise CliError(f"missing SKILL.md in {root}")
    md_bytes = skill_md.read_bytes()
    # Build a throwaway tar in memory just to reuse the same parser the
    # API uses — keeps the validation surface identical.
    import io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="SKILL.md")
        info.size = len(md_bytes)
        tf.addfile(info, io.BytesIO(md_bytes))
    try:
        parse_skill_metadata(buf.getvalue())
    except SkillBundleError as exc:
        raise CliError(f"SKILL.md is invalid: {exc}") from exc
    return md_bytes


def build_bundle(
    *,
    source_dir: Path,
    output_path: Path,
    include_hidden: bool = False,
) -> Path:
    """Validate and package a skill directory.

    Returns the path of the resulting tar.gz. The caller is responsible
    for echoing it to the operator — keeping I/O out of this helper
    makes it equally usable from a test.
    """
    _validate_input_dir(source_dir)
    files = _iter_bundle_files(source_dir, include_hidden=include_hidden)
    if not any(p.name == "SKILL.md" and p.parent == source_dir for p in files):
        # Belt + suspenders: SKILL.md must be at the bundle root.
        raise CliError(
            f"SKILL.md must live at the top of the bundle "
            f"(found nested or missing under {source_dir})"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, mode="w:gz") as tf:
        for fpath in files:
            arcname = fpath.relative_to(source_dir).as_posix()
            tf.add(fpath, arcname=arcname, recursive=False)
    return output_path


def _cmd_skill_build(args: argparse.Namespace) -> int:
    source_dir = Path(args.path).resolve()
    if args.output:
        output = Path(args.output).resolve()
    else:
        output = source_dir.with_suffix(".tar.gz") if source_dir.is_file() \
            else source_dir.parent / f"{source_dir.name}.tar.gz"
    built = build_bundle(
        source_dir=source_dir,
        output_path=output,
        include_hidden=args.include_hidden,
    )
    print(f"built {built}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse tree.

    Top-level ``linchpin`` is just a multiplexer right now —
    ``skill build`` is the only verb. Structured so future subcommands
    slot into ``add_subparsers`` without restructuring.
    """
    p = argparse.ArgumentParser(
        prog="linchpin",
        description="Linchpin command-line interface",
    )
    sub = p.add_subparsers(dest="resource", required=True)

    skill = sub.add_parser("skill", help="Skill-related commands")
    skill_sub = skill.add_subparsers(dest="action", required=True)

    build = skill_sub.add_parser(
        "build",
        help="Package a skill directory into a deployable .tar.gz bundle",
    )
    build.add_argument(
        "path",
        help="Path to the skill source directory (must contain SKILL.md)",
    )
    build.add_argument(
        "--output",
        help="Output .tar.gz path (default: <path>.tar.gz next to the source)",
    )
    build.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include dotfiles + dot-directories in the bundle (off by default)",
    )
    build.set_defaults(func=_cmd_skill_build)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
