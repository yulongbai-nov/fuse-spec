"""Tests for immutable content-addressed candidate snapshots."""

from __future__ import annotations

import copy
import importlib
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

SCRIPT_DIR = Path(__file__).parents[1] / "skills" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
fuse_candidate: Any = importlib.import_module("fuse_candidate")
fuse_record: Any = importlib.import_module("fuse_record")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def candidate_project(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    root = tmp_path / "repo"
    spec_dir = root / ".specs" / "001-demo"
    spec_dir.mkdir(parents=True)
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Fuse Test")
    git(root, "config", "user.email", "fuse@example.invalid")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (spec_dir / "requirements.md").write_text("### R1 Requirement\n", encoding="utf-8")
    (spec_dir / "design.md").write_text("### D-Core\nsatisfies: R1\n", encoding="utf-8")
    (spec_dir / "tasks.md").write_text(
        "- [ ] T1 Implement [id:T1 deps: impl:R1,D-Core]\n",
        encoding="utf-8",
    )
    (root / "fixtures").mkdir()
    (root / "fixtures" / "input.txt").write_text("fixture one\n", encoding="utf-8")
    base_config = tomllib.loads(
        (Path(__file__).parents[1] / ".fuse" / "config.toml").read_text(
            encoding="utf-8"
        )
    )
    effective_policy = {"roles": {"worker": {"model": "example-model"}}}
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial candidate")
    return root, spec_dir, base_config, effective_policy


def test_checkbox_only_and_commit_metadata_do_not_change_candidate_identity(
    tmp_path: Path,
) -> None:
    root, spec_dir, config, policy = candidate_project(tmp_path)
    record = fuse_record.new_run_record("001-demo", policy)
    first = fuse_candidate.capture_candidate(
        record, root, spec_dir, config, required_inputs=["fixtures/input.txt"]
    )
    tasks_path = spec_dir / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace("[ ]", "[x]"),
        encoding="utf-8",
    )
    git(root, "add", ".specs/001-demo/tasks.md")
    git(root, "commit", "-m", "mark task complete")
    git(root, "commit", "--allow-empty", "-m", "metadata-only commit")
    (root / ".fuse" / "runs" / record["run_id"]).mkdir(parents=True)
    (root / ".fuse" / "runs" / record["run_id"] / "run.json").write_text(
        '{"status":"completed"}\n', encoding="utf-8"
    )

    second = fuse_candidate.capture_candidate(
        record, root, spec_dir, config, required_inputs=["fixtures/input.txt"]
    )

    assert first["candidate_id"] == second["candidate_id"]
    assert len(record["candidates"]) == 1


@pytest.mark.parametrize("mutation", ["source", "spec", "policy", "checks", "fixture"])
def test_verification_input_changes_invalidate_candidate(
    tmp_path: Path, mutation: str
) -> None:
    root, spec_dir, config, policy = candidate_project(tmp_path)
    original = fuse_candidate.build_candidate_manifest(
        root, spec_dir, policy, config, required_inputs=["fixtures/input.txt"]
    )
    changed_policy = copy.deepcopy(policy)
    changed_config = copy.deepcopy(config)

    if mutation == "source":
        (root / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
    elif mutation == "spec":
        (spec_dir / "requirements.md").write_text(
            "### R1 Requirement\nChanged acceptance detail.\n", encoding="utf-8"
        )
    elif mutation == "policy":
        changed_policy["roles"]["worker"]["model"] = "another-model"
    elif mutation == "checks":
        changed_config["checks"]["pytest"]["pass_criteria"] = (
            "exit_code == 0 and approved"
        )
    elif mutation == "fixture":
        (root / "fixtures" / "input.txt").write_text("fixture two\n", encoding="utf-8")

    changed = fuse_candidate.build_candidate_manifest(
        root,
        spec_dir,
        changed_policy,
        changed_config,
        required_inputs=["fixtures/input.txt"],
    )

    assert changed != original


def test_unapproved_missing_or_escaping_candidate_inputs_fail_closed(
    tmp_path: Path,
) -> None:
    root, spec_dir, config, policy = candidate_project(tmp_path)

    with pytest.raises(fuse_candidate.CandidateError, match="missing required input"):
        fuse_candidate.build_candidate_manifest(
            root, spec_dir, policy, config, required_inputs=["fixtures/missing.txt"]
        )
    with pytest.raises(fuse_candidate.CandidateError, match="repository-relative"):
        fuse_candidate.build_candidate_manifest(
            root, spec_dir, policy, config, required_inputs=["../outside.txt"]
        )


def test_source_symlink_cannot_escape_candidate_repository(tmp_path: Path) -> None:
    root, spec_dir, config, policy = candidate_project(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("untracked dependency\n", encoding="utf-8")
    (root / "src" / "external.py").symlink_to(outside)

    with pytest.raises(fuse_candidate.CandidateError, match="symlink escapes"):
        fuse_candidate.build_candidate_manifest(root, spec_dir, policy, config)


def test_tasks_checkbox_normalizer_only_changes_declared_task_checkboxes() -> None:
    markdown = """\
- [x] T1 Complete [id:T1 deps: impl:R1]
  - [X] Nested checklist item
```md
- [x] Example [id:fake deps: impl:R1]
```
"""

    normalized = fuse_candidate.normalize_task_checkboxes(markdown)

    assert "- [ ] T1 Complete" in normalized
    assert "  - [X] Nested checklist item" in normalized
    assert "- [x] Example [id:fake" in normalized
