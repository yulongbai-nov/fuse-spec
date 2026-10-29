"""Tests for candidate-bound, project-owned verification checks."""

from __future__ import annotations

import copy
import hashlib
import importlib
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

SCRIPT_DIR = Path(__file__).parents[1] / "skills" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
fuse_candidate: Any = importlib.import_module("fuse_candidate")
fuse_checks: Any = importlib.import_module("fuse_checks")
fuse_record: Any = importlib.import_module("fuse_record")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def check_project(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
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
    template = tomllib.loads(
        (Path(__file__).parents[1] / ".fuse" / "config.toml").read_text(
            encoding="utf-8"
        )
    )
    config = copy.deepcopy(template)
    config["checks"] = {
        "tests": {
            "command": [sys.executable, "-c", "print('check passed')"],
            "prerequisites": [],
            "inputs": ["tests/**"],
            "pass_criteria": "exit_code == 0",
        },
        "lint": {
            "command": [sys.executable, "-c", "print('lint passed')"],
            "prerequisites": ["tests"],
            "inputs": ["skills/scripts/**"],
            "pass_criteria": "exit_code == 0",
        },
        "docs": {
            "command": [sys.executable, "-c", "print('docs passed')"],
            "prerequisites": [],
            "inputs": ["README.md"],
            "pass_criteria": "exit_code == 0",
        },
    }
    effective_policy = {"roles": {"worker": {"model": "example-model"}}}
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial check candidate")
    return root, spec_dir, config, effective_policy


def record_with_candidate(
    root: Path,
    spec_dir: Path,
    config: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    record = fuse_record.new_run_record("001-demo", policy)
    candidate = fuse_candidate.capture_candidate(record, root, spec_dir, config)
    return record, candidate


def append_check_evidence(
    root: Path,
    record: dict[str, Any],
    candidate_id: str,
    config: dict[str, Any],
    *,
    check_id: str,
    outcome: str,
    artifact_present: bool = True,
    artifact_entry: bool = True,
) -> dict[str, Any]:
    source_commit = git(root, "rev-parse", "HEAD")
    source = {
        "commit": source_commit,
        "branch": "main",
        "worktree_dirty": False,
        "pinned_worktree_commit": "1" * 40,
        "pinned_worktree_tree": "2" * 40,
    }
    artifact_path = f"artifacts/checks/{candidate_id}/{check_id}/manual-output.log"
    content = (
        "$ manual check\n"
        f"source_commit={source_commit}\n"
        f"pinned_commit={source['pinned_worktree_commit']}\n"
        f"pinned_tree={source['pinned_worktree_tree']}\n"
        "manual output\n"
    ).encode()
    artifacts: list[dict[str, Any]] = []
    if artifact_entry:
        artifact_root = root / ".fuse" / "runs" / record["run_id"]
        if artifact_present:
            path = artifact_root / artifact_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        artifacts.append(
            {
                "path": artifact_path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "required": True,
            }
        )
    return cast(
        dict[str, Any],
        fuse_record.add_evidence(
            record,
            candidate_id,
            "check",
            outcome,
            "manual test evidence",
            check_id=check_id,
            artifacts=artifacts,
            source_provenance=source,
            tool_versions={"python": "3.11"},
            config_digest=fuse_checks._canonical_digest(config),
            check_definition_digest=fuse_checks._canonical_digest(
                config["checks"][check_id]
            ),
        ),
    )


def test_run_check_records_candidate_bound_command_and_output_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": True,
            "summary": "locked project environment ready",
            "tool_versions": {"python": "3.11", "uv": "0.9"},
        },
    )

    result = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )

    assert result["outcome"] == "pass"
    assert result["exit_code"] == 0
    assert result["candidate_id"] == candidate["candidate_id"]
    evidence = record["evidence"][-1]
    assert evidence["candidate_id"] == candidate["candidate_id"]
    assert evidence["check_id"] == "tests"
    assert evidence["outcome"] == "pass"
    assert evidence["tool_versions"]["python"] == "3.11"
    assert evidence["config_digest"]
    assert evidence["check_definition_digest"]
    assert evidence["source_provenance"]["pinned_worktree_commit"]
    assert evidence["source_provenance"]["pinned_worktree_tree"]
    artifact_root = root / ".fuse" / "runs" / record["run_id"]
    assert fuse_record.evidence_available(evidence, artifact_root)
    artifact = artifact_root / evidence["artifacts"][0]["path"]
    assert "check passed" in artifact.read_text(encoding="utf-8")
    assert (
        fuse_checks.require_check_evidence(
            record, "tests", candidate["candidate_id"], artifact_root, config
        )
        == evidence
    )


def test_check_runs_in_detached_snapshot_of_dirty_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    original_head = git(root, "rev-parse", "HEAD")
    (root / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
    (root / "src" / "new_input.txt").write_text("untracked input\n", encoding="utf-8")
    script = (
        "import pathlib, subprocess; "
        "cwd = pathlib.Path.cwd().resolve(); "
        "head = subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, "
        "capture_output=True, text=True).stdout.strip(); "
        f"assert cwd != pathlib.Path({str(root)!r}).resolve(); "
        f"assert head != {original_head!r}; "
        "assert pathlib.Path('src/app.py').read_text() == 'value = 2\\n'; "
        "assert pathlib.Path('src/new_input.txt').read_text() == 'untracked input\\n'; "
        "print(f'cwd={cwd}'); print(f'head={head}')"
    )
    config["checks"]["tests"]["command"] = [sys.executable, "-c", script]
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    prepared_roots: list[Path] = []

    def ready_toolchain(prepared_root: Path, _config: Any) -> dict[str, Any]:
        prepared_roots.append(prepared_root)
        return {
            "ready": True,
            "summary": "locked project environment ready",
            "tool_versions": {"python": "3.11", "uv": "0.9"},
        }

    monkeypatch.setattr(fuse_checks, "prepare_toolchain", ready_toolchain)

    result = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )

    assert result["outcome"] == "pass"
    assert prepared_roots and prepared_roots[0] != root
    artifact = (
        root
        / ".fuse"
        / "runs"
        / record["run_id"]
        / record["evidence"][-1]["artifacts"][0]["path"]
    )
    output = artifact.read_text(encoding="utf-8")
    assert "cwd=" in output
    assert f"head={original_head}" not in output


def test_snapshot_materializes_ignored_required_input_and_normalizes_task_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    (root / ".gitignore").write_text("fixtures/private.txt\n", encoding="utf-8")
    fixture = root / "fixtures" / "private.txt"
    fixture.parent.mkdir()
    fixture.write_text("required but ignored\n", encoding="utf-8")
    (spec_dir / "tasks.md").write_text(
        "- [x] T1 Implement [id:T1 deps: impl:R1,D-Core]\n", encoding="utf-8"
    )
    script = (
        "import pathlib; "
        "assert pathlib.Path('fixtures/private.txt').read_text() == "
        "'required but ignored\\n'; "
        "assert '- [ ] T1 Implement' in "
        "pathlib.Path('.specs/001-demo/tasks.md').read_text(); "
        "print('candidate inputs present')"
    )
    config["checks"]["tests"]["command"] = [sys.executable, "-c", script]
    record = fuse_record.new_run_record("001-demo", policy)
    candidate = fuse_candidate.capture_candidate(
        record,
        root,
        spec_dir,
        config,
        required_inputs=["fixtures/private.txt"],
    )
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": True,
            "summary": "locked project environment ready",
            "tool_versions": {"python": "3.11", "uv": "0.9"},
        },
    )

    result = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )

    assert result["outcome"] == "pass"


def test_check_mutation_is_isolated_and_invalidates_snapshot_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    config["checks"]["tests"]["command"] = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('src/app.py').write_text('value = 99\\n')",
    ]
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": True,
            "summary": "locked project environment ready",
            "tool_versions": {},
        },
    )

    result = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )

    assert result["outcome"] == "inconclusive"
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    worktrees = git(root, "worktree", "list", "--porcelain")
    assert worktrees.count("worktree ") == 1


def test_deleted_tracked_file_is_a_pinned_candidate_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    (root / "src" / "app.py").unlink()
    config["checks"]["tests"]["command"] = [
        sys.executable,
        "-c",
        "from pathlib import Path; assert not Path('src/app.py').exists()",
    ]
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": True,
            "summary": "locked project environment ready",
            "tool_versions": {},
        },
    )

    result = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )

    assert result["outcome"] == "pass"


@pytest.mark.parametrize(
    "tampered_field",
    ["config_digest", "check_definition_digest", "pinned_worktree_tree"],
)
def test_acceptance_rejects_fabricated_digests_and_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tampered_field: str,
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": True,
            "summary": "locked project environment ready",
            "tool_versions": {"python": "3.11", "uv": "0.9"},
        },
    )
    fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )
    evidence = record["evidence"][-1]
    if tampered_field == "pinned_worktree_tree":
        evidence["source_provenance"][tampered_field] = "0" * 40
    else:
        evidence[tampered_field] = "0" * 64
    artifact_root = root / ".fuse" / "runs" / record["run_id"]

    with pytest.raises(
        fuse_checks.CheckError,
        match="digest|provenance|pinned worktree",
    ):
        fuse_checks.require_check_evidence(
            record, "tests", candidate["candidate_id"], artifact_root, config
        )
    assert (
        fuse_checks.ready_check_ids(
            config, ["lint"], record, candidate["candidate_id"], artifact_root
        )
        == []
    )


def test_check_evidence_requires_config_to_match_candidate_manifest(
    tmp_path: Path,
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    config["checks"]["tests"]["command"] = [
        sys.executable,
        "-c",
        "print('changed')",
    ]
    append_check_evidence(
        root,
        record,
        candidate["candidate_id"],
        config,
        check_id="tests",
        outcome="pass",
    )
    artifact_root = root / ".fuse" / "runs" / record["run_id"]

    with pytest.raises(fuse_checks.CheckError, match="candidate configuration context"):
        fuse_checks.require_check_evidence(
            record, "tests", candidate["candidate_id"], artifact_root, config
        )
    with pytest.raises(fuse_checks.CheckError, match="candidate configuration context"):
        fuse_checks.ready_check_ids(
            config, ["lint"], record, candidate["candidate_id"], artifact_root
        )


def test_run_check_refuses_lint_until_current_tests_evidence_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": True,
            "summary": "locked project environment ready",
            "tool_versions": {"python": "3.11", "uv": "0.9"},
        },
    )

    with pytest.raises(fuse_checks.CheckError, match="prerequisite"):
        fuse_checks.run_check(
            root, spec_dir, config, record, candidate["candidate_id"], "lint"
        )
    assert record["evidence"] == []

    tests = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )
    assert tests["outcome"] == "pass"
    lint = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "lint"
    )
    assert lint["outcome"] == "pass"


def test_each_check_execution_keeps_its_own_output_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    counter = tmp_path / "run-counter.txt"
    config["checks"]["tests"]["command"] = [
        sys.executable,
        "-c",
        "from pathlib import Path; "
        f"p = Path({str(counter)!r}); "
        "n = int(p.read_text()) if p.exists() else 0; "
        "p.write_text(str(n + 1)); print(f'run={n + 1}')",
    ]
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": True,
            "summary": "locked project environment ready",
            "tool_versions": {"python": "3.11", "uv": "0.9"},
        },
    )

    first = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )
    second = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )
    artifact_root = root / ".fuse" / "runs" / record["run_id"]
    first_evidence, second_evidence = record["evidence"]
    first_path = first_evidence["artifacts"][0]["path"]
    second_path = second_evidence["artifacts"][0]["path"]

    assert first["outcome"] == second["outcome"] == "pass"
    assert first_path != second_path
    assert fuse_record.evidence_available(first_evidence, artifact_root)
    assert fuse_record.evidence_available(second_evidence, artifact_root)
    assert "run=1" in (artifact_root / first_path).read_text(encoding="utf-8")
    assert "run=2" in (artifact_root / second_path).read_text(encoding="utf-8")


def test_output_artifact_rejects_parent_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    run_id = "run_0123456789abcdef0123456789abcdef"
    run_root = root / ".fuse" / "runs" / run_id
    run_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_root / "artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(fuse_checks.CheckError, match="outside the run directory"):
        fuse_checks._write_output_artifact(
            root, run_id, "a" * 64, "tests", b"must not escape"
        )
    assert list(outside.iterdir()) == []


def test_check_runner_rejects_stale_candidate_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": True,
            "summary": "ready",
            "tool_versions": {},
        },
    )
    (root / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")

    with pytest.raises(fuse_checks.CheckError, match="candidate is stale"):
        fuse_checks.run_check(
            root, spec_dir, config, record, candidate["candidate_id"], "tests"
        )

    assert record["evidence"] == []


def test_config_change_makes_prior_candidate_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    config["checks"]["tests"]["pass_criteria"] = "exit_code == 0 and approved"
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": True,
            "summary": "ready",
            "tool_versions": {},
        },
    )

    with pytest.raises(fuse_checks.CheckError, match="candidate is stale"):
        fuse_checks.run_check(
            root, spec_dir, config, record, candidate["candidate_id"], "tests"
        )


def test_failed_check_and_mismatched_evidence_cannot_satisfy_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    config["checks"]["tests"]["command"] = [
        sys.executable,
        "-c",
        "raise SystemExit(7)",
    ]
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": True,
            "summary": "ready",
            "tool_versions": {},
        },
    )

    result = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )
    artifact_root = root / ".fuse" / "runs" / record["run_id"]

    assert result["outcome"] == "fail"
    with pytest.raises(fuse_checks.CheckError, match="not passing"):
        fuse_checks.require_check_evidence(
            record, "tests", candidate["candidate_id"], artifact_root, config
        )
    another_candidate = fuse_record.add_candidate(record, {"source": "different"})
    with pytest.raises(fuse_checks.CheckError, match="stale candidate"):
        fuse_checks.require_check_evidence(
            record, "tests", another_candidate["candidate_id"], artifact_root, config
        )


def test_pass_without_required_output_artifact_cannot_satisfy_check(
    tmp_path: Path,
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    append_check_evidence(
        root,
        record,
        candidate["candidate_id"],
        config,
        check_id="tests",
        outcome="pass",
        artifact_entry=False,
    )
    artifact_root = root / ".fuse" / "runs" / record["run_id"]

    with pytest.raises(fuse_checks.CheckError, match="required output artifact"):
        fuse_checks.require_check_evidence(
            record, "tests", candidate["candidate_id"], artifact_root, config
        )
    assert (
        fuse_checks.ready_check_ids(
            config, ["tests"], record, candidate["candidate_id"], artifact_root
        )
        == []
    )


def test_missing_artifact_makes_passing_evidence_unavailable(
    tmp_path: Path,
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    append_check_evidence(
        root,
        record,
        candidate["candidate_id"],
        config,
        check_id="tests",
        outcome="pass",
        artifact_present=False,
    )
    artifact_root = root / ".fuse" / "runs" / record["run_id"]

    with pytest.raises(fuse_checks.CheckError, match="required artifact"):
        fuse_checks.require_check_evidence(
            record, "tests", candidate["candidate_id"], artifact_root, config
        )
    assert fuse_checks.ready_check_ids(
        config, ["lint", "docs"], record, candidate["candidate_id"], artifact_root
    ) == ["docs"]


def test_failed_prerequisite_blocks_only_dependent_checks(
    tmp_path: Path,
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    append_check_evidence(
        root,
        record,
        candidate["candidate_id"],
        config,
        check_id="tests",
        outcome="fail",
    )

    assert fuse_checks.ready_check_ids(
        config,
        ["lint", "docs"],
        record,
        candidate["candidate_id"],
        root / ".fuse" / "runs" / record["run_id"],
    ) == ["docs"]


def test_tool_restore_uses_only_selected_locked_package_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _spec_dir, config, _policy = check_project(tmp_path)
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(fuse_checks, "_validate_project_toolchain", lambda *_args: None)
    monkeypatch.setattr(fuse_checks.shutil, "which", lambda _tool: "/usr/bin/uv")

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        check: bool,
        text: bool,
        timeout: float,
    ) -> SimpleNamespace:
        calls.append((command, cwd))
        assert capture_output is True
        assert check is False
        assert text is True
        assert timeout > 0
        return SimpleNamespace(returncode=0, stdout="uv 0.9\n", stderr="")

    monkeypatch.setattr(fuse_checks.subprocess, "run", fake_run)

    result = fuse_checks.prepare_toolchain(root, config)

    assert result["ready"] is True
    assert calls[0] == (["uv", "sync", "--locked"], root)
    assert all(command[0] == "uv" for command, _cwd in calls)
    assert "npm" not in [argument for command, _cwd in calls for argument in command]


def test_restore_failure_is_unavailable_and_leaves_other_checks_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec_dir, config, policy = check_project(tmp_path)
    record, candidate = record_with_candidate(root, spec_dir, config, policy)
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda _root, _config: {
            "ready": False,
            "summary": "uv sync --locked failed",
            "tool_versions": {"uv": "0.9"},
        },
    )

    result = fuse_checks.run_check(
        root, spec_dir, config, record, candidate["candidate_id"], "tests"
    )

    assert result["outcome"] == "unavailable"
    assert record["evidence"][-1]["outcome"] == "unavailable"
    assert fuse_checks.ready_check_ids(
        config,
        ["lint", "docs"],
        record,
        candidate["candidate_id"],
        root / ".fuse" / "runs" / record["run_id"],
    ) == ["docs"]
