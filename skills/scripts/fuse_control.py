#!/usr/bin/env python3
"""Shared transition and acceptance gates for managed Fuse runs."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import fuse_candidate
    import fuse_checks
    import fuse_graph
    import fuse_record
    import fuse_state
else:
    try:
        from . import fuse_candidate, fuse_checks, fuse_graph, fuse_record, fuse_state
    except ImportError:
        import fuse_candidate
        import fuse_checks
        import fuse_graph
        import fuse_record
        import fuse_state

IMPLEMENTATION_METHODS = {
    "Bug Fix",
    "Feature",
    "Performance Issue",
    "Prototype",
    "Refactoring",
    "Runtime/Trace Forensics",
    "Visual Parity",
}
REVIEW_ROLES = {"reviewer", "rca_reviewer"}


@dataclass(frozen=True)
class TransitionDecision:
    """Machine-readable result from the shared validator."""

    allowed: bool
    reason: str
    message: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "message": self.message,
            "details": self.details,
        }


class ControlError(ValueError):
    """A control operation could not safely load or update state."""

    def __init__(
        self, reason: str, message: str, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = dict(details or {})


def _allow(message: str = "transition is valid", **details: Any) -> TransitionDecision:
    return TransitionDecision(True, "allowed", message, details)


def _deny(reason: str, message: str, **details: Any) -> TransitionDecision:
    return TransitionDecision(False, reason, message, details)


def validate_paths(repository_root: Path, spec_dir: Path) -> tuple[Path, Path]:
    """Require absolute roots and keep the approved spec within the repository."""
    if not repository_root.is_absolute() or not spec_dir.is_absolute():
        raise ControlError(
            "absolute_path_required",
            "--repo-root and --spec-dir must both be absolute paths",
        )
    try:
        root = repository_root.expanduser().resolve(strict=True)
        spec = spec_dir.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ControlError("project_path_invalid", str(error)) from error
    if not root.is_dir():
        raise ControlError("repo_root_invalid", f"not a directory: {root}")
    if not spec.is_dir():
        raise ControlError("spec_dir_invalid", f"not a directory: {spec}")
    try:
        spec.relative_to(root)
    except ValueError as error:
        raise ControlError(
            "spec_outside_repository", "spec directory must be inside the repository"
        ) from error
    return root, spec


def validate_spec_files(spec_dir: Path) -> dict[str, list[str]]:
    """Parse approved task IDs and dependencies; checkbox state is ignored."""
    if not any(
        (spec_dir / name).is_file() for name in ("requirements.md", "bugfix.md")
    ):
        raise ControlError(
            "approved_spec_incomplete", "requirements.md or bugfix.md is required"
        )
    if not (spec_dir / "design.md").is_file():
        raise ControlError("approved_spec_incomplete", "design.md is required")
    try:
        tasks = (spec_dir / "tasks.md").read_text(encoding="utf-8")
        return fuse_graph.parse_task_dependencies(tasks)
    except (OSError, UnicodeError) as error:
        raise ControlError(
            "approved_spec_incomplete", f"cannot read tasks.md: {error}"
        ) from error
    except fuse_graph.GraphError as error:
        raise ControlError("task_graph_invalid", str(error)) from error


def _validated_record(record: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if record is None:
        raise ControlError("run_required", "this operation requires a run record")
    try:
        fuse_record.validate_run_record(record)
    except fuse_record.RecordError as error:
        raise ControlError("run_record_invalid", str(error)) from error
    return record


def _task_assignments(
    record: Mapping[str, Any], task_id: str
) -> list[Mapping[str, Any]]:
    return [item for item in record["assignments"] if item["task_id"] == task_id]


def _assignment(
    record: Mapping[str, Any], assignment_id: str | None
) -> Mapping[str, Any] | None:
    return next(
        (item for item in record["assignments"] if item["id"] == assignment_id), None
    )


def _attempt(
    assignment: Mapping[str, Any], attempt_id: str | None
) -> Mapping[str, Any] | None:
    return next(
        (item for item in assignment["attempts"] if item["id"] == attempt_id), None
    )


def _latest(assignment: Mapping[str, Any]) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], assignment["attempts"][-1])


def unresolved_launches(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return still-reserved assignments that resume must reconcile."""
    rows: list[dict[str, str]] = []
    for assignment in record["assignments"]:
        if not assignment["reservation_active"]:
            continue
        attempt = _latest(assignment)
        rows.append(
            {
                "assignment_id": assignment["id"],
                "attempt_id": attempt["id"],
                "task_id": assignment["task_id"],
                "status": attempt["status"],
                "host_handle": str(attempt.get("host_handle", "")),
            }
        )
    return rows


def _manifest(
    record: Mapping[str, Any], root: Path, spec: Path, config: Mapping[str, Any]
) -> dict[str, Any]:
    required: set[str] = set()
    for candidate in record.get("candidates", ()):
        verification = candidate.get("manifest", {}).get("verification_inputs", {})
        inputs = verification.get("required_inputs", {})
        if isinstance(inputs, Mapping):
            required.update(str(path) for path in inputs)
    try:
        return fuse_candidate.build_candidate_manifest(
            root,
            spec,
            record["effective_policy_snapshot"],
            config,
            required_inputs=sorted(required),
        )
    except (fuse_candidate.CandidateError, OSError) as error:
        raise ControlError("candidate_invalid", str(error)) from error


def _candidate(
    record: Mapping[str, Any], root: Path, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    clone = copy.deepcopy(dict(record))
    try:
        return fuse_record.add_candidate(
            clone, manifest, fuse_candidate.source_provenance(root)
        )
    except (fuse_record.RecordError, fuse_candidate.CandidateError) as error:
        raise ControlError("candidate_invalid", str(error)) from error


def current_candidate(
    record: dict[str, Any], root: Path, spec: Path, config: Mapping[str, Any]
) -> dict[str, Any]:
    """Persist or reuse a candidate for the current normalized inputs."""
    manifest = _manifest(record, root, spec, config)
    try:
        return fuse_candidate.capture_candidate(
            record,
            root,
            spec,
            config,
            required_inputs=sorted(manifest["verification_inputs"]["required_inputs"]),
        )
    except (fuse_candidate.CandidateError, fuse_record.RecordError) as error:
        raise ControlError("candidate_invalid", str(error)) from error


def _snapshot(
    record: Mapping[str, Any], root: Path, spec: Path, config: Mapping[str, Any]
) -> dict[str, Any]:
    manifest = _manifest(record, root, spec, config)
    current = dict(manifest["verification_inputs"]["approved_spec"])
    saved = dict(record["spec_snapshot"]["digests"])
    changed = sorted(
        name
        for name in set(current) | set(saved)
        if current.get(name) != saved.get(name)
    )
    return {
        "changed_spec_files": changed,
        "policy_changed": record["effective_policy_snapshot"] != dict(config),
        "current_spec_digests": current,
    }


def _snapshot_gate(
    record: Mapping[str, Any], root: Path, spec: Path, config: Mapping[str, Any]
) -> TransitionDecision | None:
    try:
        state = _snapshot(record, root, spec, config)
    except ControlError as error:
        return _deny(error.reason, str(error), **error.details)
    if state["changed_spec_files"]:
        return _deny(
            "spec_snapshot_changed",
            "approved spec inputs changed; reconcile before dispatch",
            changed_spec_files=state["changed_spec_files"],
        )
    if state["policy_changed"]:
        return _deny(
            "policy_snapshot_changed", "project policy changed after run initialization"
        )
    return None


def _execution_budget_gate(record: Mapping[str, Any]) -> TransitionDecision | None:
    policy = record["effective_policy_snapshot"]
    budgets = policy.get("budgets", {})
    elapsed_limit = budgets.get("elapsed_time_seconds", "unset")
    if isinstance(elapsed_limit, int) and not isinstance(elapsed_limit, bool):
        try:
            started = datetime.fromisoformat(
                str(record["started_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            return _deny(
                "run_record_invalid", "run start time is not a valid ISO timestamp"
            )
        elapsed = (_now() - started.astimezone(UTC)).total_seconds()
        if elapsed >= elapsed_limit:
            return _deny(
                "elapsed_time_limit_reached",
                "configured elapsed-time limit has been reached",
                elapsed_seconds=int(elapsed),
                limit_seconds=elapsed_limit,
                checkpoint_required=True,
            )
    model_budget = budgets.get("model_usage", {})
    model_limit = model_budget.get("limit", "unset")
    if model_limit != "unset":
        return _deny(
            "model_usage_limit_unverifiable",
            "model usage is not recorded in the canonical run record, so dispatch "
            "cannot safely enforce the configured budget",
            limit=model_limit,
            checkpoint_required=True,
        )
    return None


def _now() -> datetime:
    return datetime.now(UTC)


def _implementation(
    record: Mapping[str, Any], task_id: str
) -> Mapping[str, Any] | None:
    matches = [
        item
        for item in _task_assignments(record, task_id)
        if item["role_profile"] not in REVIEW_ROLES
        and item["status"] == "succeeded"
        and _latest(item)["status"] == "succeeded"
    ]
    return matches[-1] if matches else None


def _review_evidence(
    record: Mapping[str, Any],
    task_id: str,
    implementation_id: str,
    candidate_id: str,
    artifact_root: Path,
) -> Mapping[str, Any] | None:
    reviewer_ids = {
        item["id"]
        for item in _task_assignments(record, task_id)
        if item["role_profile"] in REVIEW_ROLES
        and item["id"] != implementation_id
        and item["status"] == "succeeded"
        and _latest(item)["status"] == "succeeded"
    }
    rows = [
        item
        for item in record["evidence"]
        if item["kind"] == "review"
        and item["candidate_id"] == candidate_id
        and item.get("assignment_id") in reviewer_ids
    ]
    if not rows or rows[-1]["outcome"] != "pass":
        return None
    artifacts = rows[-1].get("artifacts", [])
    if not artifacts or not any(item["required"] for item in artifacts):
        return None
    return rows[-1] if fuse_record.evidence_available(rows[-1], artifact_root) else None


def _acceptance(
    record: Mapping[str, Any],
    root: Path,
    spec: Path,
    config: Mapping[str, Any],
    task_id: str | None,
    candidate_id: str | None,
) -> TransitionDecision:
    if task_id is None:
        return _deny("task_required", "accept requires a task id")
    graph = validate_spec_files(spec)
    if task_id not in graph:
        return _deny("unknown_task", f"task {task_id} is not declared in tasks.md")
    state = record["tasks"].get(task_id)
    if isinstance(state, Mapping) and state["status"] == "accepted":
        return _deny("already_accepted", f"task {task_id} is already accepted")
    assignment = _implementation(record, task_id)
    if assignment is None:
        return _deny(
            "assignment_not_succeeded",
            f"task {task_id} has no successful implementation",
        )
    gate = _snapshot_gate(record, root, spec, config)
    if gate is not None:
        return gate
    try:
        candidate = _candidate(record, root, _manifest(record, root, spec, config))
    except ControlError as error:
        return _deny(error.reason, str(error), **error.details)
    current_id = str(candidate["candidate_id"])
    if candidate_id is not None and candidate_id != current_id:
        return _deny(
            "candidate_stale",
            "requested candidate is not current",
            current_candidate_id=current_id,
        )
    checks = config.get("checks")
    if not isinstance(checks, Mapping):
        return _deny("check_policy_invalid", "project check definitions are missing")
    artifact_root = root / ".fuse" / "runs" / record["run_id"]
    check_ids: list[str] = []
    for check in sorted(checks):
        try:
            evidence = fuse_checks.require_check_evidence(
                record, str(check), current_id, artifact_root, config
            )
        except fuse_checks.CheckError as error:
            message = str(error)
            if "missing" in message:
                reason = "check_evidence_missing"
            elif "stale" in message or "candidate" in message:
                reason = "check_evidence_stale"
            elif "not passing" in message:
                reason = "check_evidence_not_passing"
            else:
                reason = "check_evidence_unavailable"
            return _deny(
                reason,
                f"required check {check}: {message}",
                check_id=check,
                candidate_id=current_id,
            )
        check_ids.append(str(evidence["id"]))
    review = _review_evidence(
        record, task_id, str(assignment["id"]), current_id, artifact_root
    )
    if review is None:
        return _deny(
            "review_evidence_missing",
            f"task {task_id} needs independent review evidence for the current "
            "candidate",
            candidate_id=current_id,
        )
    unresolved = [
        item["id"]
        for item in record["findings"]
        if item["task_id"] == task_id
        and item["blocking"]
        and item["status"] in {"open", "deferred"}
    ]
    if unresolved:
        counters = [
            item
            for item in record["review_counters"]
            if item["scope_id"] == task_id and item["level"] == "task"
        ]
        if counters and counters[-1]["repairs_used"] >= counters[-1]["limit"]:
            return _deny(
                "review_limit_exhausted",
                "task repair limit reached",
                finding_ids=unresolved,
            )
        return _deny(
            "blocking_findings_open",
            "task has unresolved blocking findings",
            finding_ids=unresolved,
        )
    return _allow(
        "task has current passing checks and independent review",
        task_id=task_id,
        candidate_id=current_id,
        assignment_id=assignment["id"],
        check_evidence_ids=check_ids,
        review_evidence_id=review["id"],
    )


def validate_transition(
    operation: str,
    *,
    repository_root: Path,
    spec_dir: Path,
    config: Mapping[str, Any],
    record: Mapping[str, Any] | None = None,
    task_id: str | None = None,
    assignment_id: str | None = None,
    attempt_id: str | None = None,
    role_profile: str | None = None,
    method: str | None = None,
    outcome: str | None = None,
    host_handle: str | None = None,
    candidate_id: str | None = None,
    check_id: str | None = None,
) -> TransitionDecision:
    """Validate every managed state transition for CLI and reconcile callers."""
    try:
        root, spec = validate_paths(repository_root, spec_dir)
        graph = validate_spec_files(spec)
    except ControlError as error:
        return _deny(error.reason, str(error), **error.details)
    if operation == "initialize":
        return _allow(spec_id=spec.name, task_ids=sorted(graph))
    try:
        current = _validated_record(record)
    except ControlError as error:
        return _deny(error.reason, str(error), **error.details)
    if current["spec_id"] != spec.name:
        return _deny("run_spec_mismatch", "run belongs to a different spec")
    status = str(current["status"])
    if operation in {"inspect", "checkpoint"}:
        return _allow(status=status, run_id=current["run_id"])
    if operation == "reconcile":
        try:
            return _allow(status=status, **_snapshot(current, root, spec, config))
        except ControlError as error:
            return _deny(error.reason, str(error), **error.details)
    if operation == "resume":
        if status in {"cancelled", "completed"}:
            return _deny("run_terminal", f"run status {status} cannot be resumed")
        snapshot = _snapshot_gate(current, root, spec, config)
        if snapshot is not None:
            return snapshot
        return _allow(status=status, unresolved_launches=unresolved_launches(current))
    if operation == "pause":
        if status != "running":
            return _deny("run_not_running", f"run status {status} cannot be paused")
        return _allow(status="paused")

    if operation in {"prepare-dispatch", "run-check", "record-review", "accept"}:
        if status != "running":
            return _deny(
                "run_not_running", f"run status {status} does not allow {operation}"
            )
        snapshot = _snapshot_gate(current, root, spec, config)
        if snapshot is not None:
            return snapshot

    if operation == "prepare-dispatch":
        if task_id is None or task_id not in graph:
            return _deny("unknown_task", f"task {task_id!r} is not declared")
        roles = config.get("roles", {})
        if role_profile not in roles:
            return _deny(
                "unknown_role_profile", f"role {role_profile!r} is not configured"
            )
        if method not in IMPLEMENTATION_METHODS | {"Review"}:
            return _deny("unknown_method", f"method {method!r} is not supported")
        budget = _execution_budget_gate(current)
        if budget is not None:
            return budget
        if any(
            item["task_id"] == task_id and item["reservation_active"]
            for item in current["assignments"]
        ):
            return _deny(
                "assignment_unresolved",
                f"task {task_id} already has an unresolved assignment",
            )
        candidate_id_for_dispatch = ""
        if role_profile in REVIEW_ROLES:
            if method != "Review":
                return _deny(
                    "review_method_required", "review assignments must use Review"
                )
            if _implementation(current, task_id) is None:
                return _deny(
                    "task_not_ready",
                    f"task {task_id} has no successful implementation to review",
                )
            try:
                candidate_id_for_dispatch = str(
                    _candidate(current, root, _manifest(current, root, spec, config))[
                        "candidate_id"
                    ]
                )
            except ControlError as error:
                return _deny(error.reason, str(error), **error.details)
        else:
            if method == "Review":
                return _deny(
                    "review_role_required", "Review requires a reviewer role profile"
                )
            eligible = fuse_graph.ready_task_ids(graph, current)
            if task_id not in eligible:
                return _deny(
                    "task_not_ready",
                    f"task {task_id} is not ready from approved dependencies and "
                    "accepted run state",
                    eligible_task_ids=eligible,
                )
        role = cast(Mapping[str, Any], roles[role_profile])
        concurrency = config["limits"]["concurrency"]
        cap = int(
            concurrency["ordinary_luna_max_floor"]
            if role["model"] == "latest-luna" and role["reasoning_effort"] == "max"
            else concurrency["other_profile_default"]
        )
        reserved = fuse_record.reserved_capacity(current)
        if reserved + 1 > cap:
            return _deny(
                "concurrency_limit_exhausted",
                "the configured shared subagent capacity is fully reserved",
                reserved_capacity=reserved,
                capacity_limit=cap,
            )
        return _allow(
            task_id=task_id,
            method=method,
            role_profile=role_profile,
            reserved_capacity=1,
            capacity_limit=cap,
            candidate_id=candidate_id_for_dispatch,
        )

    if operation in {"bind-launch", "record-result"}:
        selected = _assignment(current, assignment_id)
        if selected is None:
            return _deny(
                "unknown_assignment", f"assignment {assignment_id!r} does not exist"
            )
        selected_attempt = _attempt(selected, attempt_id)
        if selected_attempt is None:
            return _deny("unknown_attempt", f"attempt {attempt_id!r} does not exist")
        if _latest(selected)["id"] != attempt_id:
            return _deny("stale_attempt", "only the latest attempt can be changed")
        attempt_status = str(selected_attempt["status"])
        if operation == "bind-launch":
            if status != "running":
                return _deny(
                    "run_not_running",
                    f"late launch result cannot change a {status} run",
                )
            if (
                attempt_status not in {"prepared", "unknown"}
                or not selected["reservation_active"]
            ):
                return _deny(
                    "launch_not_unresolved",
                    f"attempt cannot bind from state {attempt_status}",
                )
            if not isinstance(host_handle, str) or not host_handle.strip():
                return _deny("host_handle_required", "host handle must not be empty")
            if any(
                attempt.get("host_handle") == host_handle
                for assignment in current["assignments"]
                for attempt in assignment["attempts"]
            ):
                return _deny("duplicate_host_handle", "host handle is already bound")
            return _allow(assignment_id=assignment_id, attempt_id=attempt_id)
        if status != "running":
            return _deny("run_not_running", f"late result cannot change a {status} run")
        allowed = {
            "prepared": {"unknown", "failed", "cancelled"},
            "launched": {"unknown", "succeeded", "failed", "cancelled"},
            "unknown": {"succeeded", "failed", "cancelled"},
        }
        if outcome not in allowed.get(attempt_status, set()):
            return _deny(
                "invalid_result_transition",
                f"attempt cannot transition from {attempt_status} to {outcome}",
                assignment_id=assignment_id,
                attempt_id=attempt_id,
            )
        return _allow(
            assignment_id=assignment_id, attempt_id=attempt_id, outcome=outcome
        )

    if operation == "run-check":
        if check_id is None or check_id not in config.get("checks", {}):
            return _deny("unknown_check", f"check {check_id!r} is not configured")
        try:
            current_id = str(
                _candidate(current, root, _manifest(current, root, spec, config))[
                    "candidate_id"
                ]
            )
        except ControlError as error:
            return _deny(error.reason, str(error), **error.details)
        if candidate_id is not None and candidate_id != current_id:
            return _deny(
                "candidate_stale",
                "requested candidate is not current",
                current_candidate_id=current_id,
            )
        return _allow(check_id=check_id, candidate_id=current_id)

    if operation == "record-review":
        if task_id is None or task_id not in graph:
            return _deny("unknown_task", f"task {task_id!r} is not declared")
        reviewer = _assignment(current, assignment_id)
        if (
            reviewer is None
            or reviewer["task_id"] != task_id
            or reviewer["role_profile"] not in REVIEW_ROLES
        ):
            return _deny(
                "review_assignment_invalid",
                "review evidence requires a reviewer assignment for this task",
            )
        if (
            reviewer["status"] != "succeeded"
            or _latest(reviewer)["status"] != "succeeded"
        ):
            return _deny(
                "review_assignment_incomplete", "review assignment has not succeeded"
            )
        implementation = _implementation(current, task_id)
        if implementation is None or implementation["id"] == assignment_id:
            return _deny(
                "reviewer_not_independent",
                "reviewer must differ from the implementation assignment",
            )
        try:
            current_id = str(
                _candidate(current, root, _manifest(current, root, spec, config))[
                    "candidate_id"
                ]
            )
        except ControlError as error:
            return _deny(error.reason, str(error), **error.details)
        if candidate_id != current_id:
            return _deny(
                "candidate_stale",
                "review report must match the current candidate",
                current_candidate_id=current_id,
            )
        return _allow(
            task_id=task_id, assignment_id=assignment_id, candidate_id=current_id
        )

    if operation == "accept":
        return _acceptance(current, root, spec, config, task_id, candidate_id)
    return _deny("unknown_operation", f"unknown transition operation {operation!r}")


def accept_task(
    record: dict[str, Any],
    repository_root: Path,
    spec_dir: Path,
    config: Mapping[str, Any],
    task_id: str,
    *,
    candidate_id: str | None = None,
) -> TransitionDecision:
    """Apply acceptance only when the shared current-candidate gate succeeds."""
    decision = validate_transition(
        "accept",
        repository_root=repository_root,
        spec_dir=spec_dir,
        config=config,
        record=record,
        task_id=task_id,
        candidate_id=candidate_id,
    )
    if not decision.allowed:
        return decision
    candidate = current_candidate(record, repository_root, spec_dir, config)
    expected = decision.details["candidate_id"]
    if candidate["candidate_id"] != expected:
        return _deny(
            "candidate_changed_during_acceptance",
            "candidate content changed while acceptance was being recorded",
            expected_candidate_id=expected,
            current_candidate_id=candidate["candidate_id"],
        )
    state = record["tasks"].setdefault(
        task_id,
        {"status": "pending", "assignment_ids": [], "accepted_candidate_id": None},
    )
    state["status"] = "accepted"
    state["accepted_candidate_id"] = expected
    try:
        fuse_record.validate_run_record(record)
    except fuse_record.RecordError as error:
        return _deny("run_record_invalid", str(error))
    return decision


def record_review_evidence(
    record: dict[str, Any],
    repository_root: Path,
    spec_dir: Path,
    config: Mapping[str, Any],
    task_id: str,
    assignment_id: str,
    candidate_id: str,
    summary: str,
    report: bytes,
) -> dict[str, Any]:
    """Store reviewer output as a required artifact and append its evidence ID."""
    if not summary.strip() or not report:
        raise ControlError(
            "review_report_required", "review summary and report are required"
        )
    decision = validate_transition(
        "record-review",
        repository_root=repository_root,
        spec_dir=spec_dir,
        config=config,
        record=record,
        task_id=task_id,
        assignment_id=assignment_id,
        candidate_id=candidate_id,
    )
    if not decision.allowed:
        raise ControlError(decision.reason, decision.message, decision.details)
    candidate = next(
        item for item in record["candidates"] if item["candidate_id"] == candidate_id
    )
    try:
        artifact_root = fuse_state.run_record_path(
            repository_root, record["run_id"]
        ).parent
    except fuse_state.StateError as error:
        raise ControlError("run_path_invalid", str(error)) from error
    digest = hashlib.sha256(report).hexdigest()
    relative = f"artifacts/reviews/{task_id}-{assignment_id}-{digest}.md"
    path = artifact_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != report:
        raise ControlError(
            "review_artifact_collision", "review artifact content differs"
        )
    path.write_bytes(report)
    try:
        return fuse_record.add_evidence(
            record,
            candidate_id,
            "review",
            "pass",
            summary.strip(),
            assignment_id=assignment_id,
            artifacts=[{"path": relative, "sha256": digest, "required": True}],
            source_provenance=candidate["source_provenance"],
            tool_versions={},
        )
    except fuse_record.RecordError as error:
        raise ControlError("run_record_invalid", str(error)) from error


def set_run_status(
    record: dict[str, Any],
    status: str,
    repository_root: Path,
    spec_dir: Path,
    config: Mapping[str, Any],
) -> None:
    """Pause or resume a run through the same transition validator as the CLI."""
    operation = {"paused": "pause", "running": "resume"}.get(status)
    if operation is None:
        raise ControlError(
            "invalid_run_status", f"unsupported control status transition to {status!r}"
        )
    decision = validate_transition(
        operation,
        repository_root=repository_root,
        spec_dir=spec_dir,
        config=config,
        record=record,
    )
    if not decision.allowed:
        raise ControlError(decision.reason, decision.message, decision.details)
    record["status"] = status
    try:
        fuse_record.validate_run_record(record)
    except fuse_record.RecordError as error:
        raise ControlError("run_record_invalid", str(error)) from error
