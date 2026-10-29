#!/usr/bin/env python3
"""Persist one canonical run record and checkpoint it to a fast-forward-only ref."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fuse_record import new_run_record, validate_run_record
else:
    try:
        from .fuse_record import new_run_record, validate_run_record
    except ImportError:
        from fuse_record import new_run_record, validate_run_record

SPEC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RUN_ID_RE = re.compile(r"^run_[0-9a-f]{32}$")
ACTIVE_RUN_STATUSES = {"running", "paused"}


class StateError(RuntimeError):
    """Raised when local or remote canonical run state cannot be trusted."""


class StateConflict(StateError):
    """Raised after a competing state-ref update is fetched for reconciliation."""

    def __init__(
        self,
        expected_revision: str | None,
        remote_revision: str | None,
        remote_record: dict[str, Any] | None,
        reconciliation: dict[str, Any] | None,
    ) -> None:
        super().__init__(
            "run-state ref changed concurrently; fetched the remote record for "
            "reconciliation before dispatch can continue"
        )
        self.expected_revision = expected_revision
        self.remote_revision = remote_revision
        self.remote_record = remote_record
        self.reconciliation = reconciliation


class StatePushRejected(StateError):
    """Raised when Git rejects the fast-forward push after remote reconciliation."""

    def __init__(
        self,
        message: str,
        remote_revision: str | None,
        remote_record: dict[str, Any] | None,
        reconciliation: dict[str, Any] | None,
    ) -> None:
        super().__init__(message)
        self.remote_revision = remote_revision
        self.remote_record = remote_record
        self.reconciliation = reconciliation


def _repository_root(repository_root: Path) -> Path:
    root = repository_root.expanduser().resolve()
    if not root.is_dir():
        raise StateError(f"repository root is not a directory: {root}")
    return root


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def run_record_path(repository_root: Path, run_id: str) -> Path:
    """Return the single repository-local `run.json` path, rejecting traversal."""
    if not RUN_ID_RE.fullmatch(run_id):
        raise StateError(f"invalid run id: {run_id!r}")
    root = _repository_root(repository_root)
    candidate = root / ".fuse" / "runs" / run_id / "run.json"
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except RuntimeError as error:
        raise StateError("run-record path cannot be safely resolved") from error
    except ValueError as error:
        raise StateError("run-record path resolves outside the repository") from error
    return resolved


def state_ref(spec_id: str, run_id: str) -> str:
    """Return the single remote state ref for a spec/run pair."""
    if not SPEC_ID_RE.fullmatch(spec_id) or spec_id in {".", ".."}:
        raise StateError(f"invalid spec id: {spec_id!r}")
    if not RUN_ID_RE.fullmatch(run_id):
        raise StateError(f"invalid run id: {run_id!r}")
    ref = f"refs/heads/fuse-state/{spec_id}/{run_id}"
    result = subprocess.run(
        ["git", "check-ref-format", ref], capture_output=True, check=False, text=True
    )
    if result.returncode != 0:
        raise StateError(f"invalid run-state ref for spec {spec_id!r}")
    return ref


def _git(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env is not None:
        command_env.update(env)
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        env=command_env,
        input=input_text,
        capture_output=True,
        check=False,
        text=True,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise StateError(f"git {' '.join(args)} failed: {detail}")
    return result


def load_run(repository_root: Path, run_id: str) -> dict[str, Any]:
    """Load and validate the canonical local run record without migrating it."""
    path = run_record_path(repository_root, run_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise StateError(f"run record does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"cannot read run record {path}: {error}") from error
    if not isinstance(record, dict):
        raise StateError("run record root must be an object")
    validate_run_record(record)
    if record["run_id"] != run_id:
        raise StateError("run record path and run id do not match")
    return record


def save_run(repository_root: Path, record: dict[str, Any]) -> Path:
    """Atomically update the one local run record under append-only validation."""
    root = _repository_root(repository_root)
    run_id = record.get("run_id")
    if not isinstance(run_id, str):
        raise StateError("run record has no string run_id")
    path = run_record_path(root, run_id)
    previous: dict[str, Any] | None = None
    if path.exists():
        previous = load_run(root, run_id)
    validate_run_record(record, previous=previous)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve again after directory creation so symlink substitutions fail closed.
    path = run_record_path(root, run_id)
    temp_path = path.parent / f".run.json.tmp-{uuid.uuid4().hex}"
    try:
        with temp_path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except (OSError, TypeError, ValueError) as error:
        temp_path.unlink(missing_ok=True)
        raise StateError(f"cannot write run record {path}: {error}") from error
    return path


def _runs_root(repository_root: Path) -> Path:
    root = _repository_root(repository_root)
    candidate = root / ".fuse" / "runs"
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except RuntimeError as error:
        raise StateError("run directory cannot be safely resolved") from error
    except ValueError as error:
        raise StateError("run directory resolves outside the repository") from error
    return resolved


def list_runs(
    repository_root: Path, spec_id: str | None = None
) -> list[dict[str, Any]]:
    """Load known local run records, returning them in stable run-ID order."""
    root = _repository_root(repository_root)
    runs_root = _runs_root(root)
    if not runs_root.exists():
        return []
    if not runs_root.is_dir():
        raise StateError(f"run directory is not a directory: {runs_root}")
    records: list[dict[str, Any]] = []
    for child in sorted(runs_root.iterdir(), key=lambda path: path.name):
        if not RUN_ID_RE.fullmatch(child.name):
            continue
        path = run_record_path(root, child.name)
        if not path.is_file():
            continue
        record = load_run(root, child.name)
        if spec_id is None or record["spec_id"] == spec_id:
            records.append(record)
    return records


def initialize_run(
    repository_root: Path,
    spec_id: str,
    effective_policy_snapshot: Mapping[str, Any],
    *,
    run_id: str | None = None,
    spec_revision: str | None = None,
    spec_digests: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create one canonical run after ensuring no active run already exists."""
    root = _repository_root(repository_root)
    active = [
        record
        for record in list_runs(root, spec_id)
        if record["status"] in ACTIVE_RUN_STATUSES
    ]
    if active:
        raise StateError(
            f"spec {spec_id} already has an active run; resume it instead of "
            "starting another"
        )
    new_id = run_id or f"run_{uuid.uuid4().hex}"
    path = run_record_path(root, new_id)
    if path.exists():
        raise StateError(f"run id already exists: {new_id}")
    record = new_run_record(
        spec_id,
        effective_policy_snapshot,
        run_id=new_id,
        spec_revision=spec_revision,
        spec_digests=spec_digests,
    )
    save_run(root, record)
    return record


def select_run(
    repository_root: Path, spec_id: str, run_id: str | None = None
) -> dict[str, Any]:
    """Resume an exact run or select the sole active run for a spec."""
    if run_id is not None:
        record = load_run(repository_root, run_id)
        if record["spec_id"] != spec_id:
            raise StateError(f"run {run_id} does not belong to spec {spec_id}")
        if record["status"] not in ACTIVE_RUN_STATUSES:
            raise StateError(f"run {run_id} is terminal and cannot be resumed")
        return record
    active = [
        record
        for record in list_runs(repository_root, spec_id)
        if record["status"] in ACTIVE_RUN_STATUSES
    ]
    if not active:
        raise StateError(f"no active run exists for spec {spec_id}")
    if len(active) > 1:
        raise StateError(
            f"multiple active runs exist for spec {spec_id}; provide an explicit run id"
        )
    return active[0]


def reconcile_remote(
    local_record: Mapping[str, Any], remote_record: Mapping[str, Any]
) -> dict[str, Any]:
    """Report top-level differences without silently merging either authority."""
    if (
        local_record["spec_id"] != remote_record["spec_id"]
        or local_record["run_id"] != remote_record["run_id"]
    ):
        raise StateError("cannot reconcile different run identities")
    fields = sorted(
        key
        for key in set(local_record) | set(remote_record)
        if local_record.get(key) != remote_record.get(key)
    )
    return {
        "status": "in_sync" if not fields else "conflict",
        "spec_id": local_record["spec_id"],
        "run_id": local_record["run_id"],
        "changed_fields": fields,
    }


def _fetch_remote_state(
    root: Path, remote: str | Path, spec_id: str, run_id: str
) -> tuple[str | None, dict[str, Any] | None]:
    ref = state_ref(spec_id, run_id)
    result = _git(root, "ls-remote", "--heads", str(remote), ref, check=False)
    if result.returncode != 0:
        raise StateError(f"cannot inspect run-state ref {ref}: {result.stderr.strip()}")
    if not result.stdout.strip():
        return None, None
    _git(root, "fetch", "--no-tags", str(remote), ref)
    revision = _git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
    record_path = f".fuse/runs/{run_id}/run.json"
    result = _git(root, "show", f"{revision}:{record_path}", check=False)
    if result.returncode != 0:
        raise StateError(
            f"remote state ref {ref} does not contain its canonical run record"
        )
    try:
        record_data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise StateError(f"remote run record is invalid JSON: {error}") from error
    if not isinstance(record_data, dict):
        raise StateError("remote run record root must be an object")
    validate_run_record(record_data)
    if record_data["spec_id"] != spec_id or record_data["run_id"] != run_id:
        raise StateError("remote run record identity does not match its ref")
    if record_data["git"]["state_ref"] != ref:
        raise StateError("remote run record names a different state ref")
    tree = _git(root, "ls-tree", "-r", "--name-only", revision).stdout.splitlines()
    prefix = f".fuse/runs/{run_id}/"
    if any(not item.startswith(prefix) for item in tree):
        raise StateError(
            "run-state ref contains files outside its canonical run directory"
        )
    return revision, record_data


def _build_state_commit(
    root: Path,
    record: Mapping[str, Any],
    base_revision: str | None,
    state_ref_name: str,
) -> str:
    run_id = record["run_id"]
    record_path = run_record_path(root, run_id)
    if not record_path.parent.is_dir():
        raise StateError(f"local run directory is missing: {record_path.parent}")
    relative_run_dir = f".fuse/runs/{run_id}"
    relative_record_path = f"{relative_run_dir}/run.json"
    with tempfile.TemporaryDirectory(prefix="fuse-state-index-") as temporary:
        env = {
            "GIT_INDEX_FILE": str(Path(temporary) / "index"),
            "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "Fuse Coordinator"),
            "GIT_AUTHOR_EMAIL": os.environ.get(
                "GIT_AUTHOR_EMAIL", "fuse-coordinator@example.invalid"
            ),
            "GIT_COMMITTER_NAME": os.environ.get(
                "GIT_COMMITTER_NAME", "Fuse Coordinator"
            ),
            "GIT_COMMITTER_EMAIL": os.environ.get(
                "GIT_COMMITTER_EMAIL", "fuse-coordinator@example.invalid"
            ),
        }
        if base_revision is None:
            _git(root, "read-tree", "--empty", env=env)
        else:
            _git(root, "read-tree", base_revision, env=env)
        _git(root, "add", "-f", "-A", "--", relative_run_dir, env=env)
        serialized_record = (
            json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        blob = _git(
            root, "hash-object", "-w", "--stdin", input_text=serialized_record
        ).stdout.strip()
        _git(
            root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},{relative_record_path}",
            env=env,
        )
        tree = _git(root, "write-tree", env=env).stdout.strip()
        commit_args = ["commit-tree", tree]
        if base_revision is not None:
            commit_args.extend(["-p", base_revision])
        commit_args.extend(["-m", f"Checkpoint Fuse run {run_id}"])
        commit = _git(root, *commit_args, env=env).stdout.strip()
    if not commit:
        raise StateError(f"could not create checkpoint for {state_ref_name}")
    return commit


def checkpoint_run(
    repository_root: Path,
    remote: str | Path,
    record: dict[str, Any],
    *,
    expected_revision: str | None = None,
) -> str:
    """Checkpoint to the per-run remote ref using only normal fast-forward pushes."""
    root = _repository_root(repository_root)
    validate_run_record(record)
    spec_id = record["spec_id"]
    run_id = record["run_id"]
    ref = state_ref(spec_id, run_id)
    path = run_record_path(root, run_id)
    previous: dict[str, Any] | None = None
    if path.exists():
        previous = load_run(root, run_id)
        validate_run_record(record, previous=previous)

    remote_revision, remote_record = _fetch_remote_state(root, remote, spec_id, run_id)
    if remote_revision != expected_revision:
        reconciliation = (
            reconcile_remote(record, remote_record)
            if remote_record is not None
            else None
        )
        raise StateConflict(
            expected_revision, remote_revision, remote_record, reconciliation
        )

    local_state = record["git"]
    if remote_record is None:
        if local_state["state_revision"] != 0 or local_state["state_ref"] not in {
            None,
            ref,
        }:
            raise StateConflict(expected_revision, None, None, None)
    elif (
        local_state["state_revision"] != remote_record["git"]["state_revision"]
        or local_state["state_ref"] != ref
    ):
        raise StateConflict(
            expected_revision,
            remote_revision,
            remote_record,
            reconcile_remote(record, remote_record),
        )

    if remote_record is not None:
        validate_run_record(record, previous=remote_record)

    candidate = copy.deepcopy(record)
    candidate["git"]["state_ref"] = ref
    base_state_revision = (
        remote_record["git"]["state_revision"] if remote_record is not None else 0
    )
    candidate["git"]["state_revision"] = base_state_revision + 1
    candidate["checkpointed_at"] = _now()
    validate_run_record(candidate, previous=previous)
    if remote_record is not None:
        validate_run_record(candidate, previous=remote_record)
    commit = _build_state_commit(root, candidate, remote_revision, ref)
    push = _git(root, "push", str(remote), f"{commit}:{ref}", check=False)
    if push.returncode != 0:
        fetched_revision, fetched_record = _fetch_remote_state(
            root, remote, spec_id, run_id
        )
        reconciliation = (
            reconcile_remote(candidate, fetched_record)
            if fetched_record is not None
            else None
        )
        if fetched_revision != remote_revision:
            raise StateConflict(
                remote_revision, fetched_revision, fetched_record, reconciliation
            )
        raise StatePushRejected(
            push.stderr.strip() or "run-state push was rejected",
            fetched_revision,
            fetched_record,
            reconciliation,
        )
    try:
        save_run(root, candidate)
    except StateError as error:
        raise StateError(
            f"state ref advanced to {commit}, but the local canonical record "
            f"could not be updated: {error}"
        ) from error
    record.clear()
    record.update(candidate)
    return commit
