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
import fuse_record  # noqa: E402
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


def finish_assignment(
    root: Path,
    spec_dir: Path,
    run_id: str,
    task_id: str,
    method: str,
    role_profile: str,
    host_handle: str,
) -> dict[str, Any]:
    prepared = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        task_id,
        "--method",
        method,
        "--role-profile",
        role_profile,
    )
    assert prepared.returncode == 0, prepared.stdout
    assignment = payload(prepared)
    bound = run_cli(
        root,
        spec_dir,
        "bind-launch",
        "--run-id",
        run_id,
        "--assignment-id",
        assignment["assignment_id"],
        "--attempt-id",
        assignment["attempt_id"],
        "--host-handle",
        host_handle,
    )
    assert bound.returncode == 0, bound.stdout
    completed = run_cli(
        root,
        spec_dir,
        "record-result",
        "--run-id",
        run_id,
        "--assignment-id",
        assignment["assignment_id"],
        "--attempt-id",
        assignment["attempt_id"],
        "--outcome",
        "succeeded",
        "--summary",
        f"{method} completed",
    )
    assert completed.returncode == 0, completed.stdout
    return assignment


def fail_review_with_finding(
    root: Path, spec_dir: Path, run_id: str, tmp_path: Path
) -> tuple[str, str]:
    finish_assignment(root, spec_dir, run_id, "T1", "Feature", "worker", "worker-1")
    reviewer = finish_assignment(
        root, spec_dir, run_id, "T1", "Review", "reviewer", "reviewer-1"
    )
    record = fuse_state.load_run(root, run_id)
    candidate_id = record["assignments"][-1]["attempts"][-1]["result"]["candidate_id"]
    report = tmp_path / "review-fail.md"
    report.write_text("The implementation has a blocking defect.\n", encoding="utf-8")
    failed = run_cli(
        root,
        spec_dir,
        "record-review",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--assignment-id",
        reviewer["assignment_id"],
        "--candidate-id",
        candidate_id,
        "--outcome",
        "fail",
        "--summary",
        "A blocker prevents acceptance.",
        "--finding",
        "Correct the blocking behavior.",
        "--report",
        str(report),
    )
    assert failed.returncode == 0, failed.stdout
    return candidate_id, str(payload(failed)["evidence_id"])


def submit_review(
    root: Path,
    spec_dir: Path,
    run_id: str,
    assignment_id: str,
    candidate_id: str,
    report: Path,
    *,
    outcome: str,
    summary: str,
    findings: tuple[str, ...] = (),
    repeat_finding_ids: tuple[str, ...] = (),
    resolve_finding_ids: tuple[str, ...] = (),
    resolution_rationale: str | None = None,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--assignment-id",
        assignment_id,
        "--candidate-id",
        candidate_id,
        "--outcome",
        outcome,
        "--summary",
        summary,
    ]
    for finding in findings:
        arguments.extend(("--finding", finding))
    for finding_id in repeat_finding_ids:
        arguments.extend(("--repeat-finding-id", finding_id))
    for finding_id in resolve_finding_ids:
        arguments.extend(("--resolve-finding-id", finding_id))
    if resolution_rationale is not None:
        arguments.extend(("--resolution-rationale", resolution_rationale))
    arguments.extend(("--report", str(report)))
    return run_cli(root, spec_dir, "record-review", *arguments)


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
            "--task-id",
            "T1",
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
        "--outcome",
        "pass",
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
        "    - **Files:** `src/one.py`\n"
        "- [ ] T2 Second [id:T2 deps: impl:R1,D-Test]\n"
        "    - **Files:** `src/two.py`\n"
        "- [ ] T3 Third [id:T3 deps: impl:R1,D-Test]\n"
        "    - **Files:** `src/three.py`\n"
        "- [ ] T4 Fourth [id:T4 deps: impl:R1,D-Test]\n"
        "    - **Files:** `src/four.py`\n",
    )
    config_path = root / ".fuse" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        .replace('model = "latest-luna"', 'model = "example-worker"')
        .replace("ordinary_luna_max_floor = 5", "ordinary_luna_max_floor = 2")
        .replace("other_profile_default = 3", "other_profile_default = 2"),
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


def test_review_candidate_is_pinned_through_result_and_cannot_rebind(
    tmp_path: Path,
) -> None:
    root, spec_dir = make_project(tmp_path)
    run_id = initialized_run(root, spec_dir)
    finish_assignment(root, spec_dir, run_id, "T1", "Feature", "worker", "worker-1")
    reviewer = finish_assignment(
        root, spec_dir, run_id, "T1", "Review", "reviewer", "reviewer-1"
    )
    record = fuse_state.load_run(root, run_id)
    assignment = next(
        item
        for item in record["assignments"]
        if item["id"] == reviewer["assignment_id"]
    )
    pinned_candidate = assignment["attempts"][-1]["result"]["candidate_id"]

    (root / "app.py").write_text("value = 2\n", encoding="utf-8")
    config = fuse_config.load_config(root / ".fuse" / "config.toml")
    changed_candidate = fuse_control.current_candidate(record, root, spec_dir, config)
    fuse_state.save_run(root, record)
    assert changed_candidate["candidate_id"] != pinned_candidate
    report = tmp_path / "review.md"
    report.write_text("Review report\n", encoding="utf-8")

    rebound = run_cli(
        root,
        spec_dir,
        "record-review",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--assignment-id",
        reviewer["assignment_id"],
        "--candidate-id",
        changed_candidate["candidate_id"],
        "--outcome",
        "pass",
        "--summary",
        "Review completed",
        "--report",
        str(report),
    )
    assert rebound.returncode != 0
    assert payload(rebound)["reason"] == "review_candidate_mismatch"

    stale = run_cli(
        root,
        spec_dir,
        "record-review",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--assignment-id",
        reviewer["assignment_id"],
        "--candidate-id",
        pinned_candidate,
        "--outcome",
        "pass",
        "--summary",
        "Review completed",
        "--report",
        str(report),
    )
    assert stale.returncode != 0
    assert payload(stale)["reason"] == "candidate_stale"


def test_failed_review_links_blocking_finding_and_allows_bug_fix_repair(
    tmp_path: Path,
) -> None:
    root, spec_dir = make_project(tmp_path)
    run_id = initialized_run(root, spec_dir)
    candidate_id, failed_evidence_id = fail_review_with_finding(
        root, spec_dir, run_id, tmp_path
    )
    record = fuse_state.load_run(root, run_id)
    finding = record["findings"][0]
    failed_evidence = next(
        item for item in record["evidence"] if item["id"] == failed_evidence_id
    )
    assert failed_evidence["outcome"] == "fail"
    assert failed_evidence["candidate_id"] == candidate_id
    assert failed_evidence_id in finding["evidence_ids"]
    assert failed_evidence_id in finding["history"][-1]["evidence_ids"]
    assert record["tasks"]["T1"]["status"] == "in_progress"

    repair = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "worker",
    )
    assert repair.returncode == 0, repair.stdout
    counter = fuse_state.load_run(root, run_id)["review_counters"][0]
    assert counter == {
        "scope_id": "T1",
        "level": "task",
        "repairs_used": 1,
        "limit": 2,
    }


def test_exhausted_repair_limit_still_allows_one_unspent_rca(
    tmp_path: Path,
) -> None:
    root, spec_dir = make_project(tmp_path)
    run_id = initialized_run(root, spec_dir)
    record = fuse_state.load_run(root, run_id)
    implementation = fuse_record.prepare_assignment(record, "T1", "Feature", "worker")
    attempt = implementation["attempts"][0]
    fuse_record.bind_launch(record, implementation["id"], attempt["id"], "impl-1")
    fuse_record.record_attempt_result(
        record,
        implementation["id"],
        attempt["id"],
        "succeeded",
        {"summary": "implementation complete"},
    )
    finding = fuse_record.add_finding(record, "T1", "Confirmed hard blocker")
    record["review_counters"] = [
        {"scope_id": "T1", "level": "task", "repairs_used": 2, "limit": 2}
    ]
    fuse_state.save_run(root, record)

    ordinary_repair = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "worker",
    )
    assert ordinary_repair.returncode != 0
    assert payload(ordinary_repair)["reason"] == "review_limit_exhausted"

    missing_finding = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "rca",
    )
    assert missing_finding.returncode != 0
    assert payload(missing_finding)["reason"] == "hard_blocker_required"

    rca = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "rca",
        "--finding-id",
        finding["id"],
    )
    assert rca.returncode == 0, rca.stdout
    rca_data = payload(rca)
    assert rca_data["finding_id"] == finding["id"]
    record = fuse_state.load_run(root, run_id)
    assert record["rca_attempts"] == [
        {"finding_id": finding["id"], "attempts_used": 1, "limit": 1}
    ]

    assert (
        run_cli(
            root,
            spec_dir,
            "bind-launch",
            "--run-id",
            run_id,
            "--assignment-id",
            rca_data["assignment_id"],
            "--attempt-id",
            rca_data["attempt_id"],
            "--host-handle",
            "rca-1",
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
            rca_data["assignment_id"],
            "--attempt-id",
            rca_data["attempt_id"],
            "--outcome",
            "succeeded",
        ).returncode
        == 0
    )

    exhausted_rca = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "rca",
        "--finding-id",
        finding["id"],
    )
    assert exhausted_rca.returncode != 0
    assert payload(exhausted_rca)["reason"] == "rca_limit_exhausted"


def test_passing_follow_up_resolves_only_explicit_findings_with_evidence(
    tmp_path: Path,
) -> None:
    root, spec_dir = make_project(tmp_path)
    run_id = initialized_run(root, spec_dir)
    fail_review_with_finding(root, spec_dir, run_id, tmp_path)
    finding_id = fuse_state.load_run(root, run_id)["findings"][0]["id"]
    repair = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "worker",
    )
    repair_data = payload(repair)
    assert repair.returncode == 0, repair.stdout
    assert (
        run_cli(
            root,
            spec_dir,
            "bind-launch",
            "--run-id",
            run_id,
            "--assignment-id",
            repair_data["assignment_id"],
            "--attempt-id",
            repair_data["attempt_id"],
            "--host-handle",
            "repair-1",
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
            repair_data["assignment_id"],
            "--attempt-id",
            repair_data["attempt_id"],
            "--outcome",
            "succeeded",
        ).returncode
        == 0
    )
    reviewer = finish_assignment(
        root, spec_dir, run_id, "T1", "Review", "reviewer", "reviewer-2"
    )
    report = tmp_path / "review-pass.md"
    report.write_text("The repair resolves the blocker.\n", encoding="utf-8")
    recorded = run_cli(
        root,
        spec_dir,
        "record-review",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--assignment-id",
        reviewer["assignment_id"],
        "--candidate-id",
        reviewer["candidate_id"],
        "--outcome",
        "pass",
        "--summary",
        "The targeted blocker is fixed.",
        "--resolve-finding-id",
        finding_id,
        "--resolution-rationale",
        "The current candidate passes the regression check.",
        "--report",
        str(report),
    )
    assert recorded.returncode == 0, recorded.stdout
    record = fuse_state.load_run(root, run_id)
    finding = record["findings"][0]
    evidence_id = payload(recorded)["evidence_id"]
    assert finding["status"] == "resolved"
    assert finding["history"][-1]["evidence_ids"] == [evidence_id]
    assert evidence_id in finding["evidence_ids"]
    assert record["evidence"][-1]["outcome"] == "pass"


def test_review_artifact_symlink_cannot_escape_run_root(tmp_path: Path) -> None:
    root, spec_dir = make_project(tmp_path)
    run_id = initialized_run(root, spec_dir)
    finish_assignment(root, spec_dir, run_id, "T1", "Feature", "worker", "worker-1")
    reviewer = finish_assignment(
        root, spec_dir, run_id, "T1", "Review", "reviewer", "reviewer-1"
    )
    record = fuse_state.load_run(root, run_id)
    candidate_id = next(
        attempt["result"]["candidate_id"]
        for assignment in record["assignments"]
        if assignment["id"] == reviewer["assignment_id"]
        for attempt in assignment["attempts"]
    )
    review_dir = root / ".fuse" / "runs" / run_id / "artifacts" / "reviews"
    review_dir.parent.mkdir(parents=True)
    outside = tmp_path / "outside-reviews"
    outside.mkdir()
    review_dir.symlink_to(outside, target_is_directory=True)
    report = tmp_path / "review.md"
    report.write_text("Review report\n", encoding="utf-8")
    result = run_cli(
        root,
        spec_dir,
        "record-review",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--assignment-id",
        reviewer["assignment_id"],
        "--candidate-id",
        candidate_id,
        "--outcome",
        "pass",
        "--summary",
        "Review completed",
        "--report",
        str(report),
    )
    assert result.returncode != 0
    assert payload(result)["reason"] == "artifact_path_escape"
    assert list(outside.iterdir()) == []


def test_luna_max_capacity_expands_to_independent_ready_task_count(
    tmp_path: Path,
) -> None:
    tasks = "# Tasks\n\n" + "".join(
        f"- [ ] T{index} Work {index} [id:T{index} deps: impl:R1,D-Test]\n"
        f"    - **Files:** `src/work-{index}.py`\n"
        for index in range(1, 8)
    )
    root, spec_dir = make_project(tmp_path, tasks)
    config_path = root / ".fuse" / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "ordinary_luna_max_floor = 5", "ordinary_luna_max_floor = 2"
        ),
        encoding="utf-8",
    )
    run_id = initialized_run(root, spec_dir)
    dispatches = [
        run_cli(
            root,
            spec_dir,
            "prepare-dispatch",
            "--run-id",
            run_id,
            "--task-id",
            f"T{index}",
            "--method",
            "Feature",
            "--role-profile",
            "worker",
        )
        for index in range(1, 8)
    ]
    assert all(result.returncode == 0 for result in dispatches), [
        result.stdout for result in dispatches
    ]
    assert payload(dispatches[-1])["capacity_limit"] == 7
    record = fuse_state.load_run(root, run_id)
    assert len({item["task_id"] for item in record["assignments"]}) == 7


def test_approved_t7_t8_write_scopes_cannot_be_reserved_together(
    tmp_path: Path,
) -> None:
    tasks = (
        "# Tasks\n\n"
        + "".join(
            f"- [x] T{index} Prior accepted work [id:T{index} deps: impl:R1,D-Test]\n"
            for index in range(1, 7)
        )
        + "- [ ] T7 Add serialized GitHub PR publication and exact "
        + "merge-candidate gates "
        + "[id:T7 deps:T3,T4,T5 impl:R3,D-PRIntegration,D-RunControl,D-Verification]\n"
        + "    - **Files:** `skills/scripts/fuse_git.py`, `skills/scripts/fuse_pr.py`, "
        + "`tests/test_pr_provider.py`\n"
        + "- [ ] T8 Route `/reconcile-spec` and task checkbox updates through the "
        + "shared "
        + "record [id:T8 deps:T3,T5,T6 impl:R2,R3,R4,R5,D-RunControl,D-Coordinator]\n"
        + "    - **Files:** `commands/reconcile-spec.md`, "
        + "`skills/scripts/fuse_reconcile.py`, `tests/`\n"
    )
    root, spec_dir = make_project(tmp_path, tasks)
    run_id = initialized_run(root, spec_dir)
    record = fuse_state.load_run(root, run_id)
    candidate_id = record["candidates"][0]["candidate_id"]
    for index in range(1, 7):
        record["tasks"][f"T{index}"] = {
            "status": "accepted",
            "assignment_ids": [],
            "accepted_candidate_id": candidate_id,
        }
    fuse_state.save_run(root, record)
    t7 = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T7",
        "--method",
        "Feature",
        "--role-profile",
        "worker",
    )
    assert t7.returncode == 0, t7.stdout
    t8 = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T8",
        "--method",
        "Feature",
        "--role-profile",
        "worker",
    )
    assert t8.returncode != 0
    assert payload(t8)["reason"] == "write_scope_conflict"


def test_failed_check_retries_reuse_task_finding_and_rca_allowance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, spec_dir = make_project(tmp_path)
    config_path = root / ".fuse" / "config.toml"
    original = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        original.split("[checks.pytest]", maxsplit=1)[0]
        + "[checks.smoke]\n"
        + """command = ["sh", "-c", "grep -q 'value = 2' app.py"]\n"""
        + "prerequisites = []\n"
        + 'inputs = ["app.py"]\n'
        + 'pass_criteria = "exit_code == 0"\n',
        encoding="utf-8",
    )
    git(root, "add", ".fuse/config.toml")
    git(root, "commit", "--quiet", "-m", "configure failing smoke check")
    run_id = initialized_run(root, spec_dir)
    finish_assignment(root, spec_dir, run_id, "T1", "Feature", "worker", "worker-1")
    record = fuse_state.load_run(root, run_id)
    config = fuse_config.load_config(config_path)
    candidate = fuse_control.current_candidate(record, root, spec_dir, config)
    fuse_state.save_run(root, record)
    preparation_state = {"mode": "ready"}

    def prepare_toolchain(check_root: Path, *_args: Any) -> dict[str, Any]:
        if preparation_state["mode"] == "unavailable":
            return {
                "ready": False,
                "summary": "test selected toolchain is unavailable",
                "tool_versions": {},
            }
        if preparation_state["mode"] == "inconclusive":
            (check_root / "app.py").write_text(
                "value = 'changed during preparation'\n", encoding="utf-8"
            )
        return {
            "ready": True,
            "summary": "test selected toolchain is available",
            "tool_versions": {"python": "3.11"},
        }

    monkeypatch.setattr(fuse_checks, "prepare_toolchain", prepare_toolchain)

    def run_check(candidate_id: str) -> dict[str, Any]:
        status = fuse_cli.main(
            [
                "run-check",
                "--repo-root",
                str(root),
                "--spec-dir",
                str(spec_dir),
                "--run-id",
                run_id,
                "--task-id",
                "T1",
                "--check-id",
                "smoke",
                "--candidate-id",
                candidate_id,
            ]
        )
        assert status == 0
        return payload(subprocess.CompletedProcess([], 0, capsys.readouterr().out, ""))

    preparation_state["mode"] = "unavailable"
    unavailable_result = run_check(candidate["candidate_id"])
    assert unavailable_result["outcome"] == "unavailable"
    record = fuse_state.load_run(root, run_id)
    assert record["evidence"][-1]["outcome"] == "unavailable"
    assert record["findings"] == []

    preparation_state["mode"] = "inconclusive"
    inconclusive_result = run_check(candidate["candidate_id"])
    assert inconclusive_result["outcome"] == "inconclusive"
    record = fuse_state.load_run(root, run_id)
    assert record["evidence"][-1]["outcome"] == "inconclusive"
    assert record["findings"] == []

    preparation_state["mode"] = "ready"
    first_result = run_check(candidate["candidate_id"])
    assert first_result["outcome"] == "fail"
    record = fuse_state.load_run(root, run_id)
    assert len(record["findings"]) == 1
    finding = record["findings"][0]
    finding_id = finding["id"]
    first_evidence_id = first_result["evidence_id"]
    assert finding["scope"] == "check:smoke"
    assert finding["task_id"] == "T1"
    assert finding["evidence_ids"] == [first_evidence_id]

    repair = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "worker",
    )
    assert repair.returncode == 0, repair.stdout
    repair_data = payload(repair)
    assert (
        run_cli(
            root,
            spec_dir,
            "bind-launch",
            "--run-id",
            run_id,
            "--assignment-id",
            repair_data["assignment_id"],
            "--attempt-id",
            repair_data["attempt_id"],
            "--host-handle",
            "check-repair-1",
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
            repair_data["assignment_id"],
            "--attempt-id",
            repair_data["attempt_id"],
            "--outcome",
            "succeeded",
        ).returncode
        == 0
    )
    record = fuse_state.load_run(root, run_id)
    assert record["review_counters"] == [
        {"scope_id": "T1", "level": "task", "repairs_used": 1, "limit": 2}
    ]

    rca = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "rca",
        "--finding-id",
        finding_id,
    )
    assert rca.returncode == 0, rca.stdout
    rca_data = payload(rca)
    assert (
        run_cli(
            root,
            spec_dir,
            "bind-launch",
            "--run-id",
            run_id,
            "--assignment-id",
            rca_data["assignment_id"],
            "--attempt-id",
            rca_data["attempt_id"],
            "--host-handle",
            "rca-check-1",
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
            rca_data["assignment_id"],
            "--attempt-id",
            rca_data["attempt_id"],
            "--outcome",
            "succeeded",
        ).returncode
        == 0
    )
    record_after_rca = fuse_state.load_run(root, run_id)
    assert first_evidence_id in {
        evidence["id"] for evidence in record_after_rca["evidence"]
    }
    assert first_evidence_id in record_after_rca["findings"][0]["evidence_ids"]

    second_result = run_check(candidate["candidate_id"])
    assert second_result["outcome"] == "fail"
    record = fuse_state.load_run(root, run_id)
    assert len(record["findings"]) == 1
    finding = record["findings"][0]
    assert finding["id"] == finding_id
    assert finding["evidence_ids"] == [first_evidence_id, second_result["evidence_id"]]
    assert len(finding["history"]) == 2
    assert finding["history"][-1]["action"] == "confirmed"
    assert record["rca_attempts"] == [
        {"finding_id": finding_id, "attempts_used": 1, "limit": 1}
    ]
    exhausted = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "rca",
        "--finding-id",
        finding_id,
    )
    assert exhausted.returncode != 0
    assert payload(exhausted)["reason"] == "rca_limit_exhausted"

    record = fuse_state.load_run(root, run_id)
    review_finding = fuse_record.add_finding(
        record, "T1", "Independent review issue", scope="review:independent"
    )
    fuse_state.save_run(root, record)
    (root / "app.py").write_text("value = 2\n", encoding="utf-8")
    record = fuse_state.load_run(root, run_id)
    candidate = fuse_control.current_candidate(record, root, spec_dir, config)
    fuse_state.save_run(root, record)
    passing_result = run_check(candidate["candidate_id"])
    assert passing_result["outcome"] == "pass"
    record = fuse_state.load_run(root, run_id)
    check_finding = next(
        item for item in record["findings"] if item["id"] == finding_id
    )
    assert check_finding["status"] == "resolved"
    assert check_finding["history"][-1]["action"] == "resolved"
    assert check_finding["history"][-1]["evidence_ids"] == [
        passing_result["evidence_id"]
    ]
    assert (
        next(item for item in record["findings"] if item["id"] == review_finding["id"])[
            "status"
        ]
        == "open"
    )


def test_review_replay_is_rejected_and_reopened_normalized_finding_keeps_id(
    tmp_path: Path,
) -> None:
    root, spec_dir = make_project(tmp_path)
    run_id = initialized_run(root, spec_dir)
    candidate_id, first_evidence_id = fail_review_with_finding(
        root, spec_dir, run_id, tmp_path
    )
    record = fuse_state.load_run(root, run_id)
    finding_id = record["findings"][0]["id"]
    first_reviewer = next(
        item for item in record["assignments"] if item["role_profile"] == "reviewer"
    )

    replay_report = tmp_path / "review-replay.md"
    replay_report.write_text("Replay report\n", encoding="utf-8")
    replay = submit_review(
        root,
        spec_dir,
        run_id,
        first_reviewer["id"],
        candidate_id,
        replay_report,
        outcome="fail",
        summary="Replay of the same review",
        findings=("Correct the blocking behavior.",),
    )
    assert replay.returncode != 0
    assert payload(replay)["reason"] == "review_already_recorded"
    assert len(fuse_state.load_run(root, run_id)["evidence"]) == 1

    invalid_repeat_assignment = finish_assignment(
        root,
        spec_dir,
        run_id,
        "T1",
        "Review",
        "reviewer",
        "reviewer-invalid-repeat-id",
    )
    invalid_repeat_report = tmp_path / "review-invalid-repeat.md"
    invalid_repeat_report.write_text("A rephrased blocker report.\n", encoding="utf-8")
    invalid_repeat = submit_review(
        root,
        spec_dir,
        run_id,
        invalid_repeat_assignment["assignment_id"],
        candidate_id,
        invalid_repeat_report,
        outcome="fail",
        summary="A rephrased blocker report.",
        findings=("The behavior still violates the original requirement.",),
        repeat_finding_ids=("fnd_" + "0" * 32,),
    )
    assert invalid_repeat.returncode != 0
    assert payload(invalid_repeat)["reason"] == "repeat_finding_invalid"
    assert len(fuse_state.load_run(root, run_id)["findings"]) == 1

    rca = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "rca",
        "--finding-id",
        finding_id,
    )
    assert rca.returncode == 0, rca.stdout
    rca_data = payload(rca)
    assert (
        run_cli(
            root,
            spec_dir,
            "bind-launch",
            "--run-id",
            run_id,
            "--assignment-id",
            rca_data["assignment_id"],
            "--attempt-id",
            rca_data["attempt_id"],
            "--host-handle",
            "rca-review-1",
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
            rca_data["assignment_id"],
            "--attempt-id",
            rca_data["attempt_id"],
            "--outcome",
            "succeeded",
        ).returncode
        == 0
    )

    reviewer = finish_assignment(
        root, spec_dir, run_id, "T1", "Review", "reviewer", "reviewer-after-replay"
    )
    repeated_report = tmp_path / "review-repeat-normalized.md"
    repeated_report.write_text("The same blocker is still present.\n", encoding="utf-8")
    repeated = submit_review(
        root,
        spec_dir,
        run_id,
        reviewer["assignment_id"],
        candidate_id,
        repeated_report,
        outcome="fail",
        summary="The reviewer confirms the existing blocker.",
        findings=("  correct   THE blocking behavior.  ",),
    )
    assert repeated.returncode == 0, repeated.stdout
    record = fuse_state.load_run(root, run_id)
    finding = record["findings"][0]
    assert finding["id"] == finding_id
    assert finding["scope"] == "T1"
    assert len(finding["evidence_ids"]) == 2
    assert finding["history"][-1]["action"] == "confirmed"

    reviewer = finish_assignment(
        root, spec_dir, run_id, "T1", "Review", "reviewer", "reviewer-pass-after-replay"
    )
    pass_report = tmp_path / "review-pass-before-reopen.md"
    pass_report.write_text("Verified the issue is fixed.\n", encoding="utf-8")
    passing = submit_review(
        root,
        spec_dir,
        run_id,
        reviewer["assignment_id"],
        candidate_id,
        pass_report,
        outcome="pass",
        summary="The blocker is fixed",
        resolve_finding_ids=(finding_id,),
        resolution_rationale="The current candidate passes the reproduction.",
    )
    assert passing.returncode == 0, passing.stdout

    reviewer = finish_assignment(
        root, spec_dir, run_id, "T1", "Review", "reviewer", "reviewer-reopen"
    )
    reopened_report = tmp_path / "review-reopen.md"
    reopened_report.write_text("The same issue is present again.\n", encoding="utf-8")
    reopened = submit_review(
        root,
        spec_dir,
        run_id,
        reviewer["assignment_id"],
        candidate_id,
        reopened_report,
        outcome="fail",
        summary="A follow-up report found the issue again.",
        findings=("The behavior still violates the original requirement.",),
        repeat_finding_ids=(finding_id,),
    )
    assert reopened.returncode == 0, reopened.stdout
    record = fuse_state.load_run(root, run_id)
    assert len(record["findings"]) == 1
    finding = record["findings"][0]
    assert finding["id"] == finding_id
    assert finding["status"] == "open"
    assert len(finding["evidence_ids"]) == 4
    assert finding["evidence_ids"][0] == first_evidence_id
    assert len(finding["history"]) == 4
    assert finding["history"][-1]["action"] == "reopened"
    assert record["rca_attempts"] == [
        {"finding_id": finding_id, "attempts_used": 1, "limit": 1}
    ]
    exhausted_rca = run_cli(
        root,
        spec_dir,
        "prepare-dispatch",
        "--run-id",
        run_id,
        "--task-id",
        "T1",
        "--method",
        "Bug Fix",
        "--role-profile",
        "rca",
        "--finding-id",
        finding_id,
    )
    assert exhausted_rca.returncode != 0
    assert payload(exhausted_rca)["reason"] == "rca_limit_exhausted"
