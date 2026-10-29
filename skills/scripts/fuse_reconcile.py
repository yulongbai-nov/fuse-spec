#!/usr/bin/env python3
"""Report managed spec drift and project accepted run state into task checkboxes."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import fuse_config
    import fuse_control
    import fuse_graph
    import fuse_markdown
    import fuse_record
    import fuse_state
    import plan_waves
else:
    try:
        from . import (
            fuse_config,
            fuse_control,
            fuse_graph,
            fuse_markdown,
            fuse_record,
            fuse_state,
            plan_waves,
        )
    except ImportError:
        import fuse_config
        import fuse_control
        import fuse_graph
        import fuse_markdown
        import fuse_record
        import fuse_state
        import plan_waves


class ReconcileError(RuntimeError):
    """A managed run cannot be safely reconciled or projected."""


def _record_digest(record: Mapping[str, Any]) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _config(root: Path) -> dict[str, Any]:
    config_path = root / ".fuse" / "config.toml"
    try:
        return fuse_config.load_config(config_path)
    except (fuse_config.ConfigError, OSError) as error:
        raise ReconcileError(
            f"project configuration is unavailable: {error}"
        ) from error


def _load_record(root: Path, spec: Path, run_id: str | None) -> dict[str, Any] | None:
    try:
        records = fuse_state.list_runs(root, spec.name)
    except fuse_state.StateError as error:
        raise ReconcileError(f"managed run records are invalid: {error}") from error
    if run_id is not None:
        try:
            record = fuse_state.load_run(root, run_id)
        except fuse_state.StateError as error:
            raise ReconcileError(
                f"managed run {run_id} is unavailable: {error}"
            ) from error
        if record["spec_id"] != spec.name:
            raise ReconcileError(f"run {run_id} belongs to a different spec")
        return record
    if not records:
        return None
    active = [item for item in records if item["status"] in {"running", "paused"}]
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise ReconcileError(
            "multiple active managed runs exist; pass an explicit --run-id"
        )
    raise ReconcileError(
        "managed run records exist but none is active; pass an explicit --run-id "
        "to inspect a terminal run"
    )


def _pr_key(row: Mapping[str, Any]) -> tuple[str, str, str, int]:
    return (
        str(row.get("task_id", "")),
        str(row.get("provider", "")),
        str(row.get("repository", "")),
        int(row.get("number", 0)),
    )


def compare_pr_heads(
    recorded: Sequence[Mapping[str, Any]], observed: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Compare saved and freshly observed PR identities without merging either."""
    expected_by_key = {_pr_key(row): row for row in recorded}
    observed_by_key = {_pr_key(row): row for row in observed}
    if len(expected_by_key) != len(recorded) or len(observed_by_key) != len(observed):
        raise ReconcileError("PR snapshots contain duplicate task/provider identities")
    discrepancies: list[dict[str, Any]] = []
    for key in sorted(set(expected_by_key) | set(observed_by_key)):
        expected = expected_by_key.get(key)
        current = observed_by_key.get(key)
        if expected is None:
            discrepancies.append(
                {
                    "identity": list(key),
                    "kind": "unrecorded_pr",
                    "observed": dict(current or {}),
                }
            )
        elif current is None:
            discrepancies.append(
                {
                    "identity": list(key),
                    "kind": "recorded_pr_missing",
                    "recorded": dict(expected),
                }
            )
        elif any(
            expected.get(field) != current.get(field) for field in ("base", "head")
        ):
            discrepancies.append(
                {
                    "identity": list(key),
                    "kind": "pr_head_or_base_changed",
                    "recorded": dict(expected),
                    "observed": dict(current),
                }
            )
    return discrepancies


def _read_pr_observations(path: Path | None) -> list[dict[str, Any]] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReconcileError(f"cannot read PR observation snapshot: {error}") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ReconcileError("PR observation snapshot must be a JSON array of objects")
    required = {"task_id", "provider", "repository", "number", "base", "head"}
    for item in value:
        if not required.issubset(item):
            raise ReconcileError("PR observation is missing a required identity field")
        if (
            not isinstance(item["number"], int)
            or isinstance(item["number"], bool)
            or item["number"] < 1
        ):
            raise ReconcileError("PR observation number must be a positive integer")
        if any(
            not isinstance(item[field], str) or not item[field]
            for field in required - {"number"}
        ):
            raise ReconcileError(
                "PR observation identity fields must be non-empty strings"
            )
    return value


def _task_gate_report(
    probe: Mapping[str, Any],
    root: Path,
    spec: Path,
    config: Mapping[str, Any],
    task_ids: Sequence[str],
    current_candidate_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task_id in task_ids:
        trial = copy.deepcopy(dict(probe))
        state = trial.get("tasks", {}).get(task_id)
        was_accepted = isinstance(state, Mapping) and state.get("status") == "accepted"
        record_status = (
            state.get("status", "pending") if isinstance(state, Mapping) else "pending"
        )
        prior_candidate = (
            state.get("accepted_candidate_id") if isinstance(state, Mapping) else None
        )
        if was_accepted:
            mutable_state = trial["tasks"][task_id]
            mutable_state["status"] = "in_progress"
            mutable_state["accepted_candidate_id"] = None
        decision = fuse_control.validate_transition(
            "accept",
            repository_root=root,
            spec_dir=spec,
            config=config,
            record=trial,
            task_id=task_id,
            candidate_id=current_candidate_id,
        )
        rows.append(
            {
                "task_id": task_id,
                "record_status": record_status,
                "accepted_candidate_id": prior_candidate,
                "already_accepted": was_accepted,
                "gate_allowed": decision.allowed,
                "gate_reason": decision.reason,
                "gate_message": decision.message,
                "gate_details": decision.details,
                "accepted_candidate_is_current": (
                    prior_candidate == current_candidate_id if was_accepted else None
                ),
            }
        )
    return rows


def build_report(
    repository_root: Path,
    spec_dir: Path,
    *,
    run_id: str | None = None,
    remote: str | None = None,
    observed_prs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Inspect a managed run and report drift and evidence gates without mutation."""
    try:
        root, spec = fuse_control.validate_paths(repository_root, spec_dir)
    except fuse_control.ControlError as error:
        raise ReconcileError(str(error)) from error
    record = _load_record(root, spec, run_id)
    if record is None:
        return {
            "mode": "unmanaged",
            "spec_id": spec.name,
            "message": "No managed run record exists; use legacy reconciliation.",
        }
    config = _config(root)
    decision = fuse_control.validate_transition(
        "reconcile",
        repository_root=root,
        spec_dir=spec,
        config=config,
        record=record,
    )
    if not decision.allowed:
        raise ReconcileError(
            "shared transition validator blocked reconciliation: "
            f"{decision.reason}: {decision.message}"
        )

    try:
        task_graph = fuse_control.validate_spec_files(spec)
        tasks_text = (spec / "tasks.md").read_text(encoding="utf-8")
        declarations, duplicates, checked_task_ids = plan_waves.parse_task_declarations(
            tasks_text
        )
        if duplicates:
            raise ReconcileError(
                "approved task graph has duplicate IDs: "
                + ", ".join(sorted(duplicates))
            )
        probe = copy.deepcopy(record)
        current = fuse_control.current_candidate(probe, root, spec, config)
    except (OSError, fuse_control.ControlError) as error:
        raise ReconcileError(str(error)) from error

    current_candidate_id = str(current["candidate_id"])
    known_candidate_ids = {str(item["candidate_id"]) for item in record["candidates"]}
    accepted_ids = set(fuse_graph.accepted_task_ids(record))
    current_ids = set(task_graph)
    recorded_ids = set(record["tasks"])
    unknown_record_tasks = sorted(recorded_ids - current_ids)
    missing_record_tasks = sorted(current_ids - recorded_ids)
    stale_accepted_tasks = sorted(
        task_id
        for task_id in accepted_ids
        if record["tasks"][task_id].get("accepted_candidate_id") != current_candidate_id
    )
    artifact_root = root / ".fuse" / "runs" / str(record["run_id"])
    stale_or_unavailable = []
    for item in record["evidence"]:
        try:
            available = fuse_record.evidence_available(item, artifact_root)
        except OSError:
            available = False
        if item["candidate_id"] != current_candidate_id or not available:
            stale_or_unavailable.append(item["id"])
    pr_observations = list(observed_prs) if observed_prs is not None else None
    recorded_prs = list(record["git"]["pull_requests"])
    pr_discrepancies = (
        compare_pr_heads(recorded_prs, pr_observations)
        if pr_observations is not None
        else []
    )

    result: dict[str, Any] = {
        "mode": "managed",
        "run_id": record["run_id"],
        "status": record["status"],
        "record_path": str(fuse_state.run_record_path(root, str(record["run_id"]))),
        "run_record_digest": _record_digest(record),
        **decision.details,
        "current_candidate_id": current_candidate_id,
        "candidate_is_recorded": current_candidate_id in known_candidate_ids,
        "accepted_task_ids": sorted(accepted_ids),
        "accepted_tasks_on_stale_candidate": stale_accepted_tasks,
        "checked_task_ids": sorted(checked_task_ids),
        "checked_but_unaccepted_task_ids": sorted(set(checked_task_ids) - accepted_ids),
        "accepted_but_unchecked_task_ids": sorted(accepted_ids - set(checked_task_ids)),
        "unknown_record_task_ids": unknown_record_tasks,
        "missing_record_task_ids": missing_record_tasks,
        "stale_or_unavailable_evidence_ids": stale_or_unavailable,
        "task_acceptance_gates": _task_gate_report(
            probe,
            root,
            spec,
            config,
            sorted(current_ids),
            current_candidate_id,
        ),
        "recorded_pull_requests": recorded_prs,
        "observed_pull_requests": pr_observations,
        "pull_request_discrepancies": pr_discrepancies,
        "pull_request_observation_status": (
            "checked" if pr_observations is not None else "not_checked"
        ),
        "unresolved_launches": fuse_control.unresolved_launches(record),
    }
    task_gates = result["task_acceptance_gates"]
    result["accepted_tasks_need_current_evidence"] = sorted(
        row["task_id"]
        for row in task_gates
        if row["already_accepted"]
        and (not row["accepted_candidate_is_current"] or not row["gate_allowed"])
    )
    if remote:
        try:
            revision, remote_record = fuse_state._fetch_remote_state(
                root, remote, spec.name, str(record["run_id"])
            )
        except fuse_state.StateError as error:
            raise ReconcileError(
                f"cannot inspect canonical remote state: {error}"
            ) from error
        result["remote_revision"] = revision
        result["remote_reference"] = str(remote)
        result["remote_status"] = "missing" if remote_record is None else "present"
        result["remote_reconciliation"] = (
            None
            if remote_record is None
            else fuse_state.reconcile_remote(record, remote_record)
        )
        if remote_record is not None:
            result["remote_pr_head_discrepancies"] = compare_pr_heads(
                recorded_prs, list(remote_record["git"]["pull_requests"])
            )
    else:
        result["remote_status"] = "not_checked"
        result["remote_reconciliation"] = None
        result["remote_pr_head_discrepancies"] = []
    return result


def _assert_projection_snapshot_current(
    root: Path, spec: Path, report: Mapping[str, Any]
) -> dict[str, Any]:
    """Recheck local state, candidate inputs, and any canonical remote revision."""
    run_id = str(report["run_id"])
    record = _load_record(root, spec, run_id)
    if record is None or _record_digest(record) != report.get("run_record_digest"):
        raise ReconcileError(
            "canonical local run record changed during checkbox projection; "
            "checkbox snapshot may be stale; rerun /reconcile-spec immediately"
        )
    try:
        candidate = fuse_control.current_candidate(
            copy.deepcopy(record), root, spec, _config(root)
        )
    except fuse_control.ControlError as error:
        raise ReconcileError(
            "cannot verify current candidate before checkbox projection: "
            f"{error}; rerun /reconcile-spec"
        ) from error
    if candidate["candidate_id"] != report.get("current_candidate_id"):
        raise ReconcileError(
            "approved spec or policy candidate changed during checkbox projection; "
            "checkbox snapshot may be stale; rerun /reconcile-spec immediately"
        )

    if report.get("remote_status") == "present":
        remote_reference = report.get("remote_reference")
        if not isinstance(remote_reference, str):
            raise ReconcileError("cannot revalidate the canonical remote state")
        try:
            revision, remote_record = fuse_state._fetch_remote_state(
                root, remote_reference, spec.name, run_id
            )
        except fuse_state.StateError as error:
            raise ReconcileError(
                f"cannot revalidate canonical remote state: {error}"
            ) from error
        if revision != report.get("remote_revision") or remote_record is None:
            raise ReconcileError(
                "canonical remote state changed during checkbox projection; "
                "checkbox snapshot may be stale; rerun /reconcile-spec immediately"
            )
        comparison = fuse_state.reconcile_remote(record, remote_record)
        if comparison["status"] != "in_sync":
            raise ReconcileError(
                "canonical remote state changed during checkbox projection; "
                "checkbox snapshot may be stale; rerun /reconcile-spec immediately"
            )
    return record


def _checkbox_projection_updates(
    content: bytes, accepted_task_ids: set[str]
) -> tuple[dict[str, tuple[int, bytes]], int]:
    """Find byte offsets for checkbox-only edits outside fenced code examples."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReconcileError("tasks.md is not valid UTF-8") from error
    declarations, duplicates, _checked = plan_waves.parse_task_declarations(text)
    if duplicates:
        raise ReconcileError("cannot project checkboxes for duplicate task IDs")

    updates: dict[str, tuple[int, bytes]] = {}
    seen: set[str] = set()
    byte_offset = 0
    lines = text.splitlines(keepends=True)
    fence_flags = fuse_markdown.markdown_fence_line_flags(lines)
    for line, in_fence in zip(lines, fence_flags, strict=True):
        if in_fence:
            byte_offset += len(line.encode("utf-8"))
            continue
        checkbox_row = fuse_markdown.task_checkbox_row_match(line)
        tag = plan_waves.TASK_TAG.search(line) if checkbox_row is not None else None
        if tag is not None and checkbox_row is not None:
            task_id, _deps, _impl = plan_waves.parse_tag(tag.group(1))
            if task_id and task_id in declarations:
                seen.add(task_id)
                expected = b"x" if task_id in accepted_task_ids else b" "
                if checkbox_row.group("status") != expected.decode("ascii"):
                    status_offset = (
                        byte_offset
                        + len(checkbox_row.group("bullet").encode("utf-8"))
                        + 1
                    )
                    updates[task_id] = (status_offset, expected)
        byte_offset += len(line.encode("utf-8"))

    missing_checkbox_rows = set(declarations) - seen
    if missing_checkbox_rows:
        raise ReconcileError(
            "cannot project tasks without standard checkbox rows: "
            + ", ".join(sorted(missing_checkbox_rows))
        )
    return updates, len(declarations)


def _write_checkbox_projection(
    tasks_path: Path, accepted_task_ids: Sequence[str]
) -> list[str]:
    """Write checkbox bytes under a managed-writer lock and recheck offsets."""
    accepted = set(accepted_task_ids)
    changed: set[str] = set()
    try:
        with tasks_path.open("r+b", buffering=0) as stream:
            # Other Fuse projections lock this inode too. This is advisory:
            # editors that ignore flock can write during the final window,
            # since regular-file APIs provide no content compare-and-swap.
            try:
                import fcntl
            except ImportError as error:
                raise ReconcileError(
                    "managed checkbox projection currently requires POSIX fcntl locks"
                ) from error
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            initial_content = stream.read()
            _initial_updates, task_count = _checkbox_projection_updates(
                initial_content, accepted
            )
            maximum_updates = max(1, 2 * task_count + 1)
            for _ in range(maximum_updates):
                descriptor_stat = os.fstat(stream.fileno())
                path_stat = tasks_path.stat()
                if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
                    path_stat.st_dev,
                    path_stat.st_ino,
                ):
                    raise ReconcileError(
                        "tasks.md was replaced during checkbox projection; "
                        "rerun reconcile"
                    )
                stream.seek(0)
                content = stream.read()
                updates, _current_task_count = _checkbox_projection_updates(
                    content, accepted
                )
                if not updates:
                    os.fsync(stream.fileno())
                    return sorted(changed)
                task_id = sorted(updates)[0]

                # Re-read and parse after calculating offsets. If another
                # writer inserted or removed bytes in between, retry with the
                # current layout instead of writing at a stale position.
                stream.seek(0)
                before_write = stream.read()
                current_updates, _current_task_count = _checkbox_projection_updates(
                    before_write, accepted
                )
                if before_write != content:
                    continue
                # Parsing can take long enough for another writer to change
                # this same inode. Re-read after parsing before trusting any
                # offsets returned by the parser.
                stream.seek(0)
                after_parse = stream.read()
                if after_parse != before_write:
                    continue
                current_update = current_updates.get(task_id)
                if current_update is None:
                    continue
                offset, expected = current_update
                descriptor_stat = os.fstat(stream.fileno())
                path_stat = tasks_path.stat()
                if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
                    path_stat.st_dev,
                    path_stat.st_ino,
                ):
                    raise ReconcileError(
                        "tasks.md was replaced during checkbox projection; "
                        "rerun reconcile"
                    )
                # Parsing and path checks can leave a small same-inode race.
                # Re-read as the final validation so edits made during parsing
                # retry from fresh offsets instead of corrupting unrelated text.
                stream.seek(0)
                latest_content = stream.read()
                if latest_content != before_write:
                    continue
                stream.seek(offset)
                if stream.write(expected) != len(expected):
                    raise ReconcileError("could not write a task checkbox")
                changed.add(task_id)
            raise ReconcileError(
                "tasks.md kept changing during checkbox projection; rerun reconcile"
            )
    except (OSError, ReconcileError) as error:
        if isinstance(error, ReconcileError):
            raise
        raise ReconcileError(f"cannot project task checkboxes: {error}") from error


def _project_checkboxes(
    repository_root: Path,
    spec_dir: Path,
    accepted_task_ids: Sequence[str],
    report: Mapping[str, Any],
) -> list[str]:
    """Project accepted run state into task checkboxes without changing task text."""
    blockers: list[str] = []
    if report.get("mode") != "managed":
        blockers.append("no_managed_run")
    if report.get("changed_spec_files"):
        blockers.append("approved_spec_inputs_changed")
    if report.get("policy_changed"):
        blockers.append("project_policy_changed")
    if report.get("unknown_record_task_ids"):
        blockers.append("run_has_undeclared_tasks")
    if report.get("accepted_tasks_need_current_evidence"):
        blockers.append("accepted_tasks_need_current_evidence")
    if report.get("remote_status") == "missing":
        blockers.append("canonical_remote_record_missing")
    remote_result = report.get("remote_reconciliation")
    if isinstance(remote_result, Mapping) and remote_result.get("status") != "in_sync":
        blockers.append("canonical_remote_record_conflict")
    if report.get("pull_request_discrepancies") or report.get(
        "remote_pr_head_discrepancies"
    ):
        blockers.append("pull_request_heads_disagree")
    if blockers:
        raise ReconcileError("checkbox projection blocked: " + ", ".join(blockers))

    root, spec = fuse_control.validate_paths(repository_root, spec_dir)
    _assert_projection_snapshot_current(root, spec, report)
    tasks_path = spec / "tasks.md"
    unknown_states = set(report.get("unknown_record_task_ids", ()))
    if unknown_states:
        raise ReconcileError(
            "cannot project undeclared run tasks: " + ", ".join(sorted(unknown_states))
        )
    _assert_projection_snapshot_current(root, spec, report)
    changed = _write_checkbox_projection(tasks_path, accepted_task_ids)
    try:
        _assert_projection_snapshot_current(root, spec, report)
    except ReconcileError as error:
        raise ReconcileError(
            "stale checkbox projection detected after writing; rerun "
            f"/reconcile-spec immediately: {error}"
        ) from error
    return changed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--spec-dir", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--remote", help="optional repository for canonical state comparison"
    )
    parser.add_argument(
        "--observed-prs", type=Path, help="JSON array of fresh read-only PR identities"
    )
    parser.add_argument(
        "--sync-task-checkboxes",
        action="store_true",
        help="project accepted task status from the managed run record",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        observations = _read_pr_observations(args.observed_prs)
        report = build_report(
            args.repo_root,
            args.spec_dir,
            run_id=args.run_id,
            remote=args.remote,
            observed_prs=observations,
        )
        if args.sync_task_checkboxes:
            if report.get("mode") != "managed":
                raise ReconcileError(
                    "checkbox projection is only available for managed runs"
                )
            report["projected_task_ids"] = _project_checkboxes(
                args.repo_root,
                args.spec_dir,
                report["accepted_task_ids"],
                report,
            )
        print(json.dumps({"ok": True, **report}, sort_keys=True))
        return 0
    except (ReconcileError, fuse_control.ControlError, fuse_state.StateError) as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "operation": "reconcile-spec",
                    "reason": getattr(error, "reason", "reconcile_failed"),
                    "message": str(error),
                    "details": getattr(error, "details", {}),
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
