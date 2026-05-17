"""Tests for the ``linchpin`` CLI (v0.4.0 PR3).

Exercise ``linchpin skill build`` end-to-end: a real directory tree on
disk goes in; a real tar.gz comes out; the tar.gz's frontmatter passes
the same parser the API uses on upload.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from app.cli import CliError, build_bundle, build_parser, main
from app.skills import parse_skill_metadata


def _write_skill(root: Path, name: str = "gh-pr") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "run.sh").write_text("#!/bin/sh\necho ok\n")


# ---------------------------------------------------------------------------
# build_bundle (pure)
# ---------------------------------------------------------------------------


def test_build_bundle_produces_valid_archive(tmp_path):
    src = tmp_path / "gh-pr"
    _write_skill(src)
    out = tmp_path / "out.tar.gz"
    result = build_bundle(source_dir=src, output_path=out)
    assert result == out
    assert out.exists()
    # The archive contains SKILL.md + scripts/run.sh.
    with tarfile.open(out, mode="r:gz") as tf:
        names = sorted(tf.getnames())
    assert "SKILL.md" in names
    assert "scripts/run.sh" in names
    # And the API's parser accepts it.
    name, desc = parse_skill_metadata(out.read_bytes())
    assert name == "gh-pr"
    assert desc == "Test skill."


def test_build_bundle_excludes_hidden_by_default(tmp_path):
    src = tmp_path / "gh-pr"
    _write_skill(src)
    (src / ".env").write_text("SECRET=do-not-ship\n")
    (src / ".git").mkdir()
    (src / ".git" / "config").write_text("dummy")
    out = tmp_path / "out.tar.gz"
    build_bundle(source_dir=src, output_path=out)
    with tarfile.open(out, mode="r:gz") as tf:
        names = set(tf.getnames())
    assert ".env" not in names
    assert ".git/config" not in names


def test_build_bundle_includes_hidden_when_flag_set(tmp_path):
    src = tmp_path / "gh-pr"
    _write_skill(src)
    (src / ".envfile").write_text("INCLUDE=ok\n")
    out = tmp_path / "out.tar.gz"
    build_bundle(source_dir=src, output_path=out, include_hidden=True)
    with tarfile.open(out, mode="r:gz") as tf:
        names = set(tf.getnames())
    assert ".envfile" in names


def test_build_bundle_missing_skill_md_errors(tmp_path):
    src = tmp_path / "broken"
    src.mkdir()
    (src / "other.md").write_text("nope")
    out = tmp_path / "out.tar.gz"
    with pytest.raises(CliError, match="missing SKILL.md"):
        build_bundle(source_dir=src, output_path=out)


def test_build_bundle_invalid_frontmatter_errors(tmp_path):
    src = tmp_path / "bad"
    src.mkdir()
    (src / "SKILL.md").write_text("# no frontmatter\n")
    out = tmp_path / "out.tar.gz"
    with pytest.raises(CliError, match="SKILL.md is invalid"):
        build_bundle(source_dir=src, output_path=out)


def test_build_bundle_path_not_a_directory(tmp_path):
    not_dir = tmp_path / "notadir"
    not_dir.write_text("file, not dir")
    out = tmp_path / "out.tar.gz"
    with pytest.raises(CliError, match="not a directory"):
        build_bundle(source_dir=not_dir, output_path=out)


def test_build_bundle_path_does_not_exist(tmp_path):
    out = tmp_path / "out.tar.gz"
    with pytest.raises(CliError, match="does not exist"):
        build_bundle(source_dir=tmp_path / "ghost", output_path=out)


# ---------------------------------------------------------------------------
# main() — CLI dispatch
# ---------------------------------------------------------------------------


def test_cli_skill_build_happy_path(tmp_path, capsys):
    src = tmp_path / "gh-pr"
    _write_skill(src)
    out = tmp_path / "result.tar.gz"
    rc = main(["skill", "build", str(src), "--output", str(out)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "built" in captured.out
    assert out.exists()


def test_cli_skill_build_invalid_returns_nonzero(tmp_path, capsys):
    src = tmp_path / "bad"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: claude-helper\ndescription: x\n---\n")
    rc = main(["skill", "build", str(src), "--output", str(tmp_path / "x.tar.gz")])
    assert rc == 1
    captured = capsys.readouterr()
    assert "error" in captured.err.lower()


def test_cli_skill_build_default_output_next_to_source(tmp_path, capsys):
    src = tmp_path / "gh-pr"
    _write_skill(src)
    rc = main(["skill", "build", str(src)])
    assert rc == 0
    expected = tmp_path / "gh-pr.tar.gz"
    assert expected.exists()


def test_parser_requires_subcommand(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])  # no resource → argparse errors
