"""Control CLI and transition-gate regression scenarios."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

REPOSITORY = Path(__file__).parents[1]
SCRIPT_DIR = REPOSITORY / "skills" / "scripts"
CLI = SCRIPT_DIR / "fuse_cli.py"
sys.path.insert(0, str(SCRIPT_DIR))

import fuse_checks  # noqa: E402
import fuse_cli  # noqa: E402
import fuse_config  # noqa: E402
import fuse_control  # noqa: E402
import fuse_launch  # noqa: E402
import fuse_state  # noqa: E402


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_project(tmp_path: Path, tasks: str | None = None) -> tuple[Path, Path]:
    root = tmp_path / "consumer"
    root.mkdir()
    spec_dir = root / ".specs" / "001-demo"
    spec_dir.mkdir(parents=True)
    for name, contents in {
        "requirements.md": "# Requirements\n\nA demo.\n",
        "design.md": "# Design\n\nA demo design.\n",
        "tasks.md": tasks or "# Tasks\n\n- [ ] T1 Work [id:T1 deps: impl:R1,D-Test]\n",
    }.items():
        (spec_dir / name).write_text(contents, encoding="utf-8")
    fuse_dir = root / ".fuse"
    fuse_dir.mkdir()
    (fuse_dir / "config.toml").write_text(
        (REPOSITORY / ".fuse" / "config.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for name, contents in {
        ".python-version": "3.11\n",
        "pyproject.toml": (
            '[project]\nname = "consumer"\nrequires-python = ">=3.11"\n'
        ),
        "uv.lock": (
            'version = 1\nrevision = 5\nrequires-python = ">=3.11"\n'
            '\n[[package]]\nname = "consumer"\nversion = "0.1.0"\n'
        ),
        "app.py": "value = 1\n",
    }.items():
        (root / name).write_text(contents, encoding="utf-8")
    git(root, "init", "--quiet")
    git(root, "config", "user.name", "Fuse test")
    git(root, "config", "user.email", "fuse-test@example.invalid")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "fixture")
    return root, spec_dir


def run_cli(
    root: Path,
    spec_dir: Path,
    action: str,
    *arguments: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CLI),
            action,
            "--repo-root",
            str(root),
            "--spec-dir",
            str(spec_dir),
            *arguments,
        ],
        cwd=cwd or REPOSITORY,
        capture_output=True,
        check=False,
        text=True,
    )


def payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    return cast(dict[str, Any], json.loads(result.stdout))


def initialized_run(root: Path, spec_dir: Path) -> str:
    result = run_cli(root, spec_dir, "initialize")
    assert result.returncode == 0, result.stderr
    return str(payload(result)["run_id"])


def test_cli_requires_absolute_roots_and_returns_machine_reason(tmp_path: Path) -> None:
    root, spec_dir = make_project(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "inspect",
            "--repo-root",
            ".",
            "--spec-dir",
            str(spec_dir),
        ],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode != 0
    assert json.loads(result.stdout)["reason"] == "absolute_path_required"


def test_initialize_resume_and_inspect_use_the_consumer_root(
    tmp_path: Path,
) -> None:
    root, spec_dir = make_project(tmp_path)

    created = run_cli(root, spec_dir, "initialize", cwd=tmp_path)
    assert created.returncode == 0, created.stderr
    created_data = payload(created)
    run_id = created_data["run_id"]
    assert created_data["operation"] == "initialize"
    assert created_data["record_path"] == str(
        root / ".fuse" / "runs" / run_id / "run.json"
    )

    resumed = run_cli(root, spec_dir, "resume", "--run-id", run_id, cwd=tmp_path)
    assert resumed.returncode == 0, resumed.stderr
    assert payload(resumed)["run_id"] == run_id

    inspected = run_cli(root, spec_dir, "inspect", "--run-id", run_id, cwd=tmp_path)
    assert inspected.returncode == 0, inspected.stderr
    inspected_data = payload(inspected)
    assert inspected_data["run_id"] == run_id
    assert inspected_data["eligible_task_ids"] == ["T1"]


def test_dispatch_rejects_bad_order_duplicates_and_late_results(
    tmp_path: Path,
) -> None:
    root, spec_dir = make_project(
        tmp_path,
        "# Tasks\n\n"
        "- [ ] T1 First [id:T1 deps: impl:R1,D-Test]\n"
        "- [ ] T2 Second [id:T2 deps:T1 impl:R1,D-Test]\n",
    )
    run_id = initialized_run(root, spec_dir)

    blocked = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T2",
        "--method",
        "Feature",
        "--role-profile",
        "worker",
    )
    assert blocked.returncode != 0
    assert payload(blocked)["reason"] == "task_not_ready"

    prepared = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Feature",
        "--role-profile",
        "worker",
    )
    assert prepared.returncode == 0, prepared.stderr
    assignment_id = payload(prepared)["assignment_id"]
    attempt_id = payload(prepared)["attempt_id"]

    duplicate = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Feature",
        "--role-profile",
        "worker",
    )
    assert duplicate.returncode != 0
    assert payload(duplicate)["reason"] == "assignment_unresolved"

    uncertain = run_cli(
        root,
        spec_dir,
        "record-result",
        "--run-id",
        run_id,
        "--assignment-id",
        assignment_id,
        "--attempt-id",
        attempt_id,
        "--outcome",
        "unknown",
        "--summary",
        "launch response was lost",
    )
    assert uncertain.returncode == 0, uncertain.stderr
    resumed = run_cli(root, spec_dir, "resume", "--run-id", run_id)
    assert payload(resumed)["unresolved_launches"][0]["attempt_id"] == attempt_id

    rebound = run_cli(
        root,
        spec_dir,
        "bind-launch",
        "--run-id",
        run_id,
        "--assignment-id",
        assignment_id,
        "--attempt-id",
        attempt_id,
        "--host-handle",
        "host-123",
    )
    assert rebound.returncode == 0, rebound.stderr

    completed = run_cli(
        root,
        spec_dir,
        "record-result",
        "--run-id",
        run_id,
        "--assignment-id",
        assignment_id,
        "--attempt-id",
        attempt_id,
        "--outcome",
        "succeeded",
        "--summary",
        "the assigned change is ready for verification",
    )
    assert completed.returncode == 0, completed.stderr
    late = run_cli(
        root,
        spec_dir,
        "record-result",
        "--run-id",
        run_id,
        "--assignment-id",
        assignment_id,
        "--attempt-id",
        attempt_id,
        "--outcome",
        "failed",
        "--summary",
        "late result",
    )
    assert late.returncode != 0
    assert payload(late)["reason"] == "invalid_result_transition"


def test_acceptance_ignores_checked_box_and_blocks_missing_evidence(
    tmp_path: Path,
) -> None:
    root, spec_dir = make_project(
        tmp_path, "# Tasks\n\n- [x] T1 Work [id:T1 deps: impl:R1,D-Test]\n"
    )
    run_id = initialized_run(root, spec_dir)
    assignment = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Feature",
        "--role-profile",
        "worker",
    )
    assignment_id = payload(assignment)["assignment_id"]
    attempt_id = payload(assignment)["attempt_id"]
    run_cli(
        root,
        spec_dir,
        "bind-launch",
        "--run-id",
        run_id,
        "--assignment-id",
        assignment_id,
        "--attempt-id",
        attempt_id,
        "--host-handle",
        "host-1",
    )
    result = run_cli(
        root,
        spec_dir,
        "record-result",
        "--run-id",
        run_id,
        "--assignment-id",
        assignment_id,
        "--attempt-id",
        attempt_id,
        "--outcome",
        "succeeded",
        "--summary",
        "claimed done",
    )
    assert result.returncode == 0, result.stderr

    accepted = run_cli(root, spec_dir, "accept", "--run-id", run_id, "--task-id", "T1")
    assert accepted.returncode != 0
    assert payload(accepted)["reason"] == "check_evidence_missing"

    record = json.loads(
        (root / ".fuse" / "runs" / run_id / "run.json").read_text(encoding="utf-8")
    )
    assert record["tasks"]["T1"]["status"] == "in_progress"
    assert record["tasks"]["T1"]["accepted_candidate_id"] is None


def test_acceptance_succeeds_with_current_check_and_independent_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, spec_dir = make_project(tmp_path)
    config_path = root / ".fuse" / "config.toml"
    original = config_path.read_text(encoding="utf-8")
    prefix = original.split("[checks.pytest]", maxsplit=1)[0]
    config_path.write_text(
        prefix
        + "[checks.smoke]\n"
        + 'command = ["python3", "-c", "print(\\"check passed\\")"]\n'
        + "prerequisites = []\n"
        + 'inputs = ["app.py"]\n'
        + 'pass_criteria = "exit_code == 0"\n',
        encoding="utf-8",
    )
    git(root, "add", ".fuse/config.toml")
    git(root, "commit", "--quiet", "-m", "configure smoke check")
    run_id = initialized_run(root, spec_dir)

    worker = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Feature",
        "--role-profile",
        "worker",
    )
    worker_data = payload(worker)
    assert worker.returncode == 0, worker.stderr
    assignment_id = worker_data["assignment_id"]
    attempt_id = worker_data["attempt_id"]
    assert (
        run_cli(
            root,
            spec_dir,
            "bind-launch",
            "--run-id",
            run_id,
            "--assignment-id",
            assignment_id,
            "--attempt-id",
            attempt_id,
            "--host-handle",
            "worker-1",
        ).returncode
        == 0
    )
    assert (
        run_cli(
            root,
            spec_dir,
            "record-result",
            "--run-id",
            run_id,
            "--assignment-id",
            assignment_id,
            "--attempt-id",
            attempt_id,
            "--outcome",
            "succeeded",
            "--summary",
            "implementation returned",
        ).returncode
        == 0
    )

    config = fuse_config.load_config(config_path)
    record = fuse_state.load_run(root, run_id)
    candidate = fuse_control.current_candidate(record, root, spec_dir, config)
    fuse_state.save_run(root, record)
    monkeypatch.setattr(
        fuse_checks,
        "prepare_toolchain",
        lambda *_args: {
            "ready": True,
            "summary": "test selected toolchain is available",
            "tool_versions": {"python": "3.11"},
        },
    )
    check_status = fuse_cli.main(
        [
            "run-check",
            "--repo-root",
            str(root),
            "--spec-dir",
            str(spec_dir),
            "--run-id",
            run_id,
            "--check-id",
            "smoke",
            "--candidate-id",
            candidate["candidate_id"],
        ]
    )
    assert check_status == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "pass"
    record = fuse_state.load_run(root, run_id)
    fuse_state.save_run(root, record)

    reviewer = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Review",
        "--role-profile",
        "reviewer",
    )
    reviewer_data = payload(reviewer)
    assert reviewer.returncode == 0, reviewer.stderr
    reviewer_id = reviewer_data["assignment_id"]
    reviewer_attempt = reviewer_data["attempt_id"]
    assert reviewer_data["candidate_id"] == candidate["candidate_id"]
    assert (
        run_cli(
            root,
            spec_dir,
            "bind-launch",
            "--run-id",
            run_id,
            "--assignment-id",
            reviewer_id,
            "--attempt-id",
            reviewer_attempt,
            "--host-handle",
            "reviewer-1",
        ).returncode
        == 0
    )
    assert (
        run_cli(
            root,
            spec_dir,
            "record-result",
            "--run-id",
            run_id,
            "--assignment-id",
            reviewer_id,
            "--attempt-id",
            reviewer_attempt,
            "--outcome",
            "succeeded",
            "--summary",
            "review completed",
        ).returncode
        == 0
    )

    report = tmp_path / "review.md"
    report.write_text(
        "Reviewed candidate and found no blocking issue.\n", encoding="utf-8"
    )
    recorded_review = run_cli(
        root,
        spec_dir,
        "record-review",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--assignment-id",
        reviewer_id,
        "--candidate-id",
        candidate["candidate_id"],
        "--summary",
        "independent review passed",
        "--report",
        str(report),
    )
    assert recorded_review.returncode == 0, recorded_review.stderr

    accepted = run_cli(
        root,
        spec_dir,
        "accept",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--candidate-id",
        candidate["candidate_id"],
    )
    assert accepted.returncode == 0, accepted.stdout
    assert payload(accepted)["candidate_id"] == candidate["candidate_id"]
    final_record = fuse_state.load_run(root, run_id)
    assert final_record["tasks"]["T1"]["status"] == "accepted"


def test_dispatch_enforces_shared_concurrency_limit(tmp_path: Path) -> None:
    root, spec_dir = make_project(
        tmp_path,
        "# Tasks\n\n"
        "- [ ] T1 First [id:T1 deps: impl:R1,D-Test]\n"
        "- [ ] T2 Second [id:T2 deps: impl:R1,D-Test]\n"
        "- [ ] T3 Third [id:T3 deps: impl:R1,D-Test]\n"
        "- [ ] T4 Fourth [id:T4 deps: impl:R1,D-Test]\n",
    )
    config_path = root / ".fuse" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "ordinary_luna_max_floor = 5", "ordinary_luna_max_floor = 2"
        ),
        encoding="utf-8",
    )
    git(root, "add", ".fuse/config.toml")
    git(root, "commit", "--quiet", "-m", "set concurrency cap")
    run_id = initialized_run(root, spec_dir)

    for task_id in ("T1", "T2"):
        response = run_cli(
            root,
            spec_dir,
            "prepare-dispatch",
            "--run-id",
            run_id,
            "--task-id",
            task_id,
            "--method",
            "Feature",
            "--role-profile",
            "worker",
        )
        assert response.returncode == 0, response.stderr

    exhausted = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T3",
        "--method",
        "Feature",
        "--role-profile",
        "worker",
    )
    assert exhausted.returncode != 0
    assert payload(exhausted)["reason"] == "concurrency_limit_exhausted"


def test_checkpoint_and_reconcile_use_the_consumer_repository_state(
    tmp_path: Path,
) -> None:
    root, spec_dir = make_project(tmp_path)
    remote = tmp_path / "state-remote.git"
    git(tmp_path, "init", "--bare", "--quiet", str(remote))
    run_id = initialized_run(root, spec_dir)

    checkpoint = run_cli(
        root,
        spec_dir,
        "checkpoint",
        "--run-id",
        run_id,
        "--remote",
        str(remote),
        cwd=tmp_path,
    )
    assert checkpoint.returncode == 0, checkpoint.stderr
    checkpoint_data = payload(checkpoint)
    assert checkpoint_data["state_revision"] == 1
    assert checkpoint_data["state_ref"] == f"refs/heads/fuse-state/001-demo/{run_id}"

    reconciled = run_cli(
        root,
        spec_dir,
        "reconcile",
        "--run-id",
        run_id,
        "--remote",
        str(remote),
        cwd=tmp_path,
    )
    assert reconciled.returncode == 0, reconciled.stderr
    reconciliation = payload(reconciled)
    assert reconciliation["remote_status"] == "present"
    assert reconciliation["remote_reconciliation"]["status"] == "in_sync"
    assert reconciliation["changed_spec_files"] == []

    tasks = spec_dir / "tasks.md"
    tasks.write_text(
        tasks.read_text(encoding="utf-8").replace("T1 Work", "T1 Changed"),
        encoding="utf-8",
    )
    drift = run_cli(root, spec_dir, "reconcile", "--run-id", run_id)
    assert drift.returncode == 0, drift.stderr
    assert payload(drift)["changed_spec_files"] == ["tasks.md"]


def test_launcher_resolves_roots_before_changing_to_plugin_root(tmp_path: Path) -> None:
    root, _spec_dir = make_project(tmp_path)
    repo_root, spec_dir = fuse_launch.resolve_roots(
        tmp_path, str(root), ".specs/001-demo"
    )
    assert repo_root == root.resolve()
    assert spec_dir == (root / ".specs/001-demo").resolve()


def test_launcher_passes_resolved_roots_to_uv_from_plugin_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _spec_dir = make_project(tmp_path)
    captured: dict[str, Any] = {}

    def fake_run(
        command: list[str], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["cwd"] = cwd
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: "/fake/bin/uv")
    monkeypatch.setattr(subprocess, "run", fake_run)
    status = fuse_launch.main(
        [
            "initialize",
            "--repo-root",
            "consumer",
            "--spec-dir",
            ".specs/001-demo",
        ]
    )

    command = captured["command"]
    plugin_root = SCRIPT_DIR.parents[1].resolve()
    assert status == 0
    assert captured["cwd"] == plugin_root
    assert command[0] == "/fake/bin/uv"
    assert command[command.index("--project") + 1] == str(plugin_root)
    assert command[command.index("--directory") + 1] == str(plugin_root)
    assert command[-4:] == [
        "--repo-root",
        str(root.resolve()),
        "--spec-dir",
        str((root / ".specs/001-demo").resolve()),
    ]
