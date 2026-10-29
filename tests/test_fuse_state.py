"""Local-Git tests for the single-writer state backend."""

from __future__ import annotations

import importlib
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_DIR = Path(__file__).parents[1] / "skills" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
fuse_state: Any = importlib.import_module("fuse_state")


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository_with_bare_remote(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    remote = tmp_path / "remote.git"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    git(repository, "config", "user.name", "Fuse Test")
    git(repository, "config", "user.email", "fuse@example.invalid")
    (repository / "source.txt").write_text("source\n", encoding="utf-8")
    git(repository, "add", "source.txt")
    git(repository, "commit", "-m", "initial source")
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "-u", "origin", "main")
    return repository, remote


def test_local_run_record_directory_is_ignored_by_feature_commits() -> None:
    repository = Path(__file__).parents[1]
    result = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "--quiet",
            ".fuse/runs/run_" + "0" * 32 + "/run.json",
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0


def competitor_advance(
    tmp_path: Path, remote: Path, ref: str, run_id: str, *, push: bool = True
) -> tuple[Path, str]:
    competitor = tmp_path / "competitor"
    subprocess.run(
        ["git", "clone", str(remote), str(competitor)],
        check=True,
        capture_output=True,
    )
    git(competitor, "config", "user.name", "Fuse Competitor")
    git(competitor, "config", "user.email", "competitor@example.invalid")
    git(competitor, "fetch", "origin", ref)
    git(competitor, "checkout", "-B", "state-update", "FETCH_HEAD")
    record_path = fuse_state.run_record_path(competitor, run_id)
    data = json.loads(record_path.read_text(encoding="utf-8"))
    data["status"] = "paused"
    data["git"]["state_revision"] += 1
    record_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    git(competitor, "add", record_path.relative_to(competitor).as_posix())
    git(competitor, "commit", "-m", "competing state update")
    if push:
        git(competitor, "push", "origin", f"HEAD:{ref}")
        revision = git(remote, "rev-parse", ref)
    else:
        revision = git(competitor, "rev-parse", "HEAD")
    return competitor, revision


def test_initialize_run_has_one_canonical_record_and_stable_remote_ref(
    tmp_path: Path,
) -> None:
    repository, _ = repository_with_bare_remote(tmp_path)
    record = fuse_state.initialize_run(
        repository,
        "001-demo",
        {"roles": {"worker": {"model": "example-model"}}},
    )

    path = fuse_state.run_record_path(repository, record["run_id"])

    assert path == repository / ".fuse" / "runs" / record["run_id"] / "run.json"
    assert path.is_file()
    assert fuse_state.state_ref("001-demo", record["run_id"]) == (
        f"refs/heads/fuse-state/001-demo/{record['run_id']}"
    )
    assert fuse_state.load_run(repository, record["run_id"]) == record


def test_checkpoint_uses_ff_only_remote_ref_without_touching_source_branch(
    tmp_path: Path,
) -> None:
    repository, remote = repository_with_bare_remote(tmp_path)
    record = fuse_state.initialize_run(
        repository,
        "001-demo",
        {"roles": {"worker": {"model": "example-model"}}},
    )
    ref = fuse_state.state_ref(record["spec_id"], record["run_id"])
    source_head = git(repository, "rev-parse", "HEAD")

    revision = fuse_state.checkpoint_run(repository, remote, record)

    assert git(remote, "rev-parse", ref) == revision
    assert git(repository, "rev-parse", "HEAD") == source_head
    assert git(remote, "ls-tree", "-r", "--name-only", ref) == (
        f".fuse/runs/{record['run_id']}/run.json"
    )


def test_competing_update_is_fetched_and_reconciled_before_more_work(
    tmp_path: Path,
) -> None:
    repository, remote = repository_with_bare_remote(tmp_path)
    record = fuse_state.initialize_run(
        repository,
        "001-demo",
        {"roles": {"worker": {"model": "example-model"}}},
    )
    ref = fuse_state.state_ref(record["spec_id"], record["run_id"])
    original_revision = fuse_state.checkpoint_run(repository, remote, record)
    _, competing_revision = competitor_advance(tmp_path, remote, ref, record["run_id"])
    record["status"] = "completed"

    with pytest.raises(fuse_state.StateConflict) as caught:
        fuse_state.checkpoint_run(
            repository, remote, record, expected_revision=original_revision
        )

    conflict = caught.value
    assert conflict.remote_revision == competing_revision
    assert conflict.remote_record is not None
    assert conflict.remote_record["status"] == "paused"
    reconciliation = fuse_state.reconcile_remote(record, conflict.remote_record)
    assert reconciliation["status"] == "conflict"
    assert "status" in reconciliation["changed_fields"]
    assert git(remote, "rev-parse", ref) == competing_revision


def test_checkpoint_rejects_stale_expected_revision(tmp_path: Path) -> None:
    repository, remote = repository_with_bare_remote(tmp_path)
    record = fuse_state.initialize_run(repository, "001-demo", {"roles": {}})
    ref = fuse_state.state_ref(record["spec_id"], record["run_id"])
    original_revision = fuse_state.checkpoint_run(repository, remote, record)
    _, competing_revision = competitor_advance(tmp_path, remote, ref, record["run_id"])
    record["status"] = "paused"

    with pytest.raises(fuse_state.StateConflict):
        fuse_state.checkpoint_run(
            repository, remote, record, expected_revision=original_revision
        )

    assert git(remote, "rev-parse", ref) == competing_revision


def test_latest_remote_sha_does_not_authorize_checkpoint_from_stale_local_state(
    tmp_path: Path,
) -> None:
    repository, remote = repository_with_bare_remote(tmp_path)
    record = fuse_state.initialize_run(repository, "001-demo", {"roles": {}})
    ref = fuse_state.state_ref(record["spec_id"], record["run_id"])
    fuse_state.checkpoint_run(repository, remote, record)
    _, competing_revision = competitor_advance(tmp_path, remote, ref, record["run_id"])
    record["status"] = "completed"

    with pytest.raises(fuse_state.StateConflict) as caught:
        fuse_state.checkpoint_run(
            repository, remote, record, expected_revision=competing_revision
        )

    assert caught.value.remote_revision == competing_revision
    assert caught.value.remote_record is not None
    assert caught.value.remote_record["status"] == "paused"
    assert git(remote, "rev-parse", ref) == competing_revision


def test_missing_local_record_cannot_bypass_remote_policy_immutability(
    tmp_path: Path,
) -> None:
    repository, remote = repository_with_bare_remote(tmp_path)
    record = fuse_state.initialize_run(repository, "001-demo", {"roles": {}})
    revision = fuse_state.checkpoint_run(repository, remote, record)
    path = fuse_state.run_record_path(repository, record["run_id"])
    path.unlink()
    record["effective_policy_snapshot"]["roles"]["worker"] = {"model": "changed"}

    with pytest.raises(ValueError, match="effective policy snapshot is immutable"):
        fuse_state.checkpoint_run(
            repository, remote, record, expected_revision=revision
        )

    assert (
        git(remote, "rev-parse", fuse_state.state_ref("001-demo", record["run_id"]))
        == revision
    )
    assert not path.exists()


def test_missing_local_record_allows_update_based_on_fetched_remote_state(
    tmp_path: Path,
) -> None:
    repository, remote = repository_with_bare_remote(tmp_path)
    record = fuse_state.initialize_run(repository, "001-demo", {"roles": {}})
    initial_revision = fuse_state.checkpoint_run(repository, remote, record)
    fuse_state.run_record_path(repository, record["run_id"]).unlink()
    record["status"] = "paused"

    revision = fuse_state.checkpoint_run(
        repository, remote, record, expected_revision=initial_revision
    )

    assert (
        git(remote, "rev-parse", fuse_state.state_ref("001-demo", record["run_id"]))
        == revision
    )
    assert record["status"] == "paused"
    assert fuse_state.load_run(repository, record["run_id"])["status"] == "paused"


def test_non_fast_forward_push_fetches_and_reconciles_competing_update(
    tmp_path: Path,
) -> None:
    repository, remote = repository_with_bare_remote(tmp_path)
    record = fuse_state.initialize_run(repository, "001-demo", {"roles": {}})
    ref = fuse_state.state_ref(record["spec_id"], record["run_id"])
    original_revision = fuse_state.checkpoint_run(repository, remote, record)
    competitor, competing_revision = competitor_advance(
        tmp_path, remote, ref, record["run_id"], push=False
    )
    pre_push = repository / ".git" / "hooks" / "pre-push"
    pre_push.write_text(
        "#!/bin/sh\n"
        f"git -C {shlex.quote(str(competitor))} push origin HEAD:{shlex.quote(ref)}\n",
        encoding="utf-8",
    )
    pre_push.chmod(0o755)
    record["status"] = "completed"

    with pytest.raises(fuse_state.StateConflict) as caught:
        fuse_state.checkpoint_run(
            repository, remote, record, expected_revision=original_revision
        )

    assert caught.value.remote_revision == competing_revision
    assert caught.value.remote_record is not None
    assert caught.value.remote_record["status"] == "paused"
    assert caught.value.reconciliation["status"] == "conflict"
    assert git(remote, "rev-parse", ref) == competing_revision
    local_record = fuse_state.load_run(repository, record["run_id"])
    assert local_record["status"] == "running"
    assert local_record["git"]["state_revision"] == 1


def test_run_paths_reject_traversal_and_remote_refs_are_safe() -> None:
    with pytest.raises(fuse_state.StateError, match="invalid run id"):
        fuse_state.run_record_path(Path("/tmp/repo"), "../outside")
    with pytest.raises(fuse_state.StateError, match="invalid spec id"):
        fuse_state.state_ref("../outside", "run_123")
