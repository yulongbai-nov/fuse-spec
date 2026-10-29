"""Shared helpers for tests that need local Git repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_minimal_spec_repository(
    tmp_path: Path,
    *,
    include_fixture: bool,
    commit_message: str,
) -> tuple[Path, Path]:
    """Create the shared source/spec repository used by candidate and check tests."""
    root = tmp_path / "repo"
    spec_dir = root / ".specs" / "001-demo"
    spec_dir.mkdir(parents=True)
    source_dir = root / "src"
    source_dir.mkdir()
    (source_dir / "app.py").write_text("value = 1\n", encoding="utf-8")
    (spec_dir / "requirements.md").write_text("### R1 Requirement\n", encoding="utf-8")
    (spec_dir / "design.md").write_text("### D-Core\nsatisfies: R1\n", encoding="utf-8")
    (spec_dir / "tasks.md").write_text(
        "- [ ] T1 Implement [id:T1 deps: impl:R1,D-Core]\n",
        encoding="utf-8",
    )
    if include_fixture:
        fixture_dir = root / "fixtures"
        fixture_dir.mkdir()
        (fixture_dir / "input.txt").write_text("fixture one\n", encoding="utf-8")

    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Fuse Test")
    git(root, "config", "user.email", "fuse-test@example.invalid")
    git(root, "add", "-A")
    git(root, "commit", "-m", commit_message)
    return root, spec_dir
