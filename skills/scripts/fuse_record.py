#!/usr/bin/env python3
"""Build and validate Fuse's canonical, versioned execution record."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

SCHEMA_VERSION = 1
TERMINAL_ATTEMPT_STATES = {"succeeded", "failed", "cancelled"}
RESERVED_ATTEMPT_STATES = {"prepared", "launched", "unknown"}
DISPATCH_CONTEXT_FIELDS = (
    "candidate_id",
    "finding_id",
    "rca_base_candidate_id",
    "rca_attempt_ids",
)


class RecordError(ValueError):
    """Raised when a run record violates its versioned contract."""


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_schema_path() -> Path:
    return _plugin_root() / "schemas" / "run-record.schema.json"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _candidate_digest(manifest: Mapping[str, Any]) -> str:
    payload = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _schema_errors(schema_path: Path | None) -> Draft202012Validator:
    path = schema_path or _default_schema_path()
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecordError(f"cannot read run-record schema {path}: {error}") from error
    if not isinstance(schema, dict):
        raise RecordError(f"run-record schema must be an object: {path}")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise RecordError(
            f"invalid run-record schema {path}: {error.message}"
        ) from error
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _format_error(error: ValidationError) -> str:
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{location}: {error.message}"


def _unique_ids(
    items: Sequence[Mapping[str, Any]], field: str, label: str
) -> list[str]:
    ids = [item[field] for item in items]
    duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicates:
        raise RecordError(f"duplicate {label} id(s): {', '.join(duplicates)}")
    return ids


def _semantic_errors(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    assignments = record["assignments"]
    candidates = record["candidates"]
    evidence = record["evidence"]
    findings = record["findings"]
    tasks = record["tasks"]

    assignment_ids = set(_unique_ids(assignments, "id", "assignment"))
    candidate_ids = set(_unique_ids(candidates, "candidate_id", "candidate"))
    evidence_ids = set(_unique_ids(evidence, "id", "evidence"))
    finding_ids = set(_unique_ids(findings, "id", "finding"))
    _unique_ids(record["operator_decisions"], "id", "operator decision")
    attempt_ids: set[str] = set()
    host_handles: set[str] = set()
    active_task_ids: set[str] = set()

    for assignment in assignments:
        task_id = assignment["task_id"]
        if task_id not in tasks:
            errors.append(f"assignment {assignment['id']} has no task-state entry")
        latest = assignment["attempts"][-1]
        if assignment["status"] != latest["status"]:
            errors.append(
                f"assignment {assignment['id']} status does not match "
                "its latest attempt"
            )
        should_be_reserved = latest["status"] in RESERVED_ATTEMPT_STATES
        if assignment["reservation_active"] != should_be_reserved:
            errors.append(
                f"assignment {assignment['id']} reservation does not match launch state"
            )
        if should_be_reserved:
            if task_id in active_task_ids:
                errors.append(f"task {task_id} has multiple active assignments")
            active_task_ids.add(task_id)
        for attempt in assignment["attempts"]:
            attempt_id = attempt["id"]
            if attempt_id in attempt_ids:
                errors.append(f"duplicate attempt id: {attempt_id}")
            attempt_ids.add(attempt_id)
            host_handle = attempt.get("host_handle")
            if host_handle is not None:
                if host_handle in host_handles:
                    errors.append(f"duplicate host handle: {host_handle}")
                host_handles.add(host_handle)
            result = attempt.get("result", {})
            if isinstance(result, Mapping):
                for field in (
                    "candidate_id",
                    "rca_base_candidate_id",
                    "rca_candidate_id",
                ):
                    candidate_id = result.get(field)
                    if candidate_id is not None and (
                        not isinstance(candidate_id, str)
                        or candidate_id not in candidate_ids
                    ):
                        errors.append(
                            f"attempt {attempt_id} references unknown candidate "
                            f"{candidate_id}"
                        )
                finding_id = result.get("finding_id")
                if finding_id is not None and finding_id not in finding_ids:
                    errors.append(
                        f"attempt {attempt_id} references unknown finding {finding_id}"
                    )

    task_assignment_ids: set[str] = set()
    rca_attempt_ids = {
        attempt["id"]
        for assignment in assignments
        if assignment["role_profile"] == "rca"
        for attempt in assignment["attempts"]
    }
    for assignment in assignments:
        for attempt in assignment["attempts"]:
            references = attempt.get("result", {}).get("rca_attempt_ids", [])
            if (
                not isinstance(references, list)
                or any(
                    not isinstance(item, str) or item not in rca_attempt_ids
                    for item in references
                )
                or len(references) != len(set(references))
            ):
                errors.append(
                    "RCA review context must reference unique RCA attempt IDs"
                )

    for task_id, task_state in tasks.items():
        for assignment_id in task_state["assignment_ids"]:
            if assignment_id not in assignment_ids:
                errors.append(
                    f"task {task_id} references unknown assignment {assignment_id}"
                )
            elif assignment_id in task_assignment_ids:
                errors.append(f"assignment {assignment_id} is listed by multiple tasks")
            else:
                task_assignment_ids.add(assignment_id)
        for assignment in assignments:
            if (
                assignment["task_id"] == task_id
                and assignment["id"] not in task_state["assignment_ids"]
            ):
                errors.append(
                    f"task {task_id} does not reference assignment {assignment['id']}"
                )
        accepted_candidate = task_state["accepted_candidate_id"]
        if accepted_candidate is not None and accepted_candidate not in candidate_ids:
            errors.append(
                f"task {task_id} references unknown candidate {accepted_candidate}"
            )

    for item in evidence:
        if item["candidate_id"] not in candidate_ids:
            errors.append(
                f"evidence {item['id']} references unknown candidate "
                f"{item['candidate_id']}"
            )
        assignment_id = item.get("assignment_id")
        if assignment_id is not None and assignment_id not in assignment_ids:
            errors.append(
                f"evidence {item['id']} references unknown assignment {assignment_id}"
            )
    for candidate in candidates:
        if candidate["candidate_id"] != _candidate_digest(candidate["manifest"]):
            errors.append(
                f"candidate id does not match its manifest: {candidate['candidate_id']}"
            )
    for finding in findings:
        latest_action = finding["history"][-1]["action"]
        expected_status = {
            "opened": "open",
            "confirmed": "open",
            "reopened": "open",
            "resolved": "resolved",
            "dismissed": "dismissed",
            "deferred": "deferred",
        }[latest_action]
        if finding["status"] != expected_status:
            errors.append(
                f"finding {finding['id']} status does not match latest "
                "disposition action"
            )
        for evidence_id in finding["evidence_ids"]:
            if evidence_id not in evidence_ids:
                errors.append(
                    f"finding {finding['id']} references unknown evidence {evidence_id}"
                )
        for entry in finding["history"]:
            for evidence_id in entry["evidence_ids"]:
                if evidence_id not in evidence_ids:
                    errors.append(
                        f"finding {finding['id']} history references unknown evidence "
                        f"{evidence_id}"
                    )
    for rca_attempt in record["rca_attempts"]:
        if rca_attempt["finding_id"] not in finding_ids:
            errors.append(
                f"RCA attempt references unknown finding {rca_attempt['finding_id']}"
            )
    review_keys = [
        (counter["scope_id"], counter["level"]) for counter in record["review_counters"]
    ]
    if len(review_keys) != len(set(review_keys)):
        errors.append("duplicate review counter for scope and level")
    for counter in record["review_counters"]:
        if counter["repairs_used"] > counter["limit"]:
            errors.append(f"review counter for {counter['scope_id']} exceeds its limit")
    rca_finding_ids = [item["finding_id"] for item in record["rca_attempts"]]
    if len(rca_finding_ids) != len(set(rca_finding_ids)):
        errors.append("duplicate RCA counter for finding")
    for rca_attempt in record["rca_attempts"]:
        if rca_attempt["attempts_used"] > rca_attempt["limit"]:
            errors.append(
                f"RCA counter for {rca_attempt['finding_id']} exceeds its limit"
            )
    return errors


def _require_prefix(
    old_items: Sequence[Mapping[str, Any]],
    new_items: Sequence[Mapping[str, Any]],
    field: str,
    label: str,
) -> None:
    old_ids = [item[field] for item in old_items]
    new_ids = [item[field] for item in new_items]
    if new_ids[: len(old_ids)] != old_ids:
        raise RecordError(f"{label} history is append-only")


def _validate_counter_history(
    old_rows: Sequence[Mapping[str, Any]],
    new_rows: Sequence[Mapping[str, Any]],
    identity_fields: tuple[str, ...],
    usage_field: str,
    label: str,
) -> None:
    old_keys = [tuple(row[field] for field in identity_fields) for row in old_rows]
    new_keys = [tuple(row[field] for field in identity_fields) for row in new_rows]
    if new_keys[: len(old_keys)] != old_keys:
        raise RecordError(f"{label} counter history is append-only")
    for old_row, new_row in zip(old_rows, new_rows):
        if old_row["limit"] != new_row["limit"]:
            raise RecordError(f"{label} counter limit is immutable")
        if new_row[usage_field] < old_row[usage_field]:
            raise RecordError(f"{label} counter usage is monotonic")


def _validate_history(previous: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    if (
        previous["spec_id"] != current["spec_id"]
        or previous["run_id"] != current["run_id"]
    ):
        raise RecordError("run identity is immutable")
    if previous["effective_policy_snapshot"] != current["effective_policy_snapshot"]:
        raise RecordError("effective policy snapshot is immutable")
    if previous["spec_snapshot"] != current["spec_snapshot"]:
        raise RecordError("approved spec snapshot is immutable")

    old_assignments = previous["assignments"]
    new_assignments = current["assignments"]
    _require_prefix(old_assignments, new_assignments, "id", "assignment")
    for old_assignment, new_assignment in zip(old_assignments, new_assignments):
        for field in ("task_id", "method", "role_profile", "reserved_capacity"):
            if old_assignment[field] != new_assignment[field]:
                raise RecordError(f"assignment {field} is immutable")
        old_attempts = old_assignment["attempts"]
        new_attempts = new_assignment["attempts"]
        _require_prefix(old_attempts, new_attempts, "id", "attempt")
        if (
            len(new_attempts) > len(old_attempts)
            and old_attempts[-1]["status"] not in TERMINAL_ATTEMPT_STATES
        ):
            raise RecordError("cannot retry while the previous attempt is unresolved")
        for old_attempt, new_attempt in zip(old_attempts, new_attempts):
            old_result = old_attempt.get("result", {})
            new_result = new_attempt.get("result", {})
            for field in DISPATCH_CONTEXT_FIELDS:
                if (
                    isinstance(old_result, Mapping)
                    and field in old_result
                    and (
                        not isinstance(new_result, Mapping)
                        or new_result.get(field) != old_result[field]
                    )
                ):
                    raise RecordError(f"attempt dispatch {field} is immutable")
            old_host_handle = old_attempt.get("host_handle")
            if (
                old_host_handle is not None
                and new_attempt.get("host_handle") != old_host_handle
            ):
                raise RecordError("attempt host handle is immutable once bound")
            if (
                old_attempt["status"] in TERMINAL_ATTEMPT_STATES
                and old_attempt != new_attempt
            ):
                raise RecordError("terminal attempt history is immutable")

    for field, id_field, label in (
        ("candidates", "candidate_id", "candidate"),
        ("evidence", "id", "evidence"),
    ):
        old_items = previous[field]
        new_items = current[field]
        _require_prefix(old_items, new_items, id_field, label)
        for old_item, new_item in zip(old_items, new_items):
            if old_item != new_item:
                raise RecordError(f"{label} history is immutable")

    old_findings = previous["findings"]
    new_findings = current["findings"]
    _require_prefix(old_findings, new_findings, "id", "finding")
    for old_finding, new_finding in zip(old_findings, new_findings):
        if old_finding["task_id"] != new_finding["task_id"]:
            raise RecordError("finding task identity is immutable")
        if old_finding["scope"] != new_finding["scope"]:
            raise RecordError("finding scope identity is immutable")
        old_history = old_finding["history"]
        new_history = new_finding["history"]
        if new_history[: len(old_history)] != old_history:
            raise RecordError("finding disposition history is append-only")

    _validate_counter_history(
        previous["review_counters"],
        current["review_counters"],
        ("scope_id", "level"),
        "repairs_used",
        "review",
    )
    _validate_counter_history(
        previous["rca_attempts"],
        current["rca_attempts"],
        ("finding_id",),
        "attempts_used",
        "RCA",
    )

    old_decisions = previous["operator_decisions"]
    new_decisions = current["operator_decisions"]
    _require_prefix(old_decisions, new_decisions, "id", "operator decision")
    for old_decision, new_decision in zip(old_decisions, new_decisions):
        if old_decision != new_decision:
            raise RecordError("operator decision history is immutable")


def validate_run_record(
    record: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
    schema_path: Path | None = None,
) -> None:
    """Validate schema, unique identities, references, and update immutability."""
    version = record.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise RecordError(f"unsupported run-record schema version: {version!r}")
    try:
        json.dumps(dict(record), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RecordError(
            f"run record must contain finite JSON values: {error}"
        ) from error

    validator = _schema_errors(schema_path)
    schema_errors = sorted(
        validator.iter_errors(dict(record)),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if schema_errors:
        details = "; ".join(_format_error(error) for error in schema_errors)
        raise RecordError(f"run-record schema validation failed: {details}")
    semantic_errors = _semantic_errors(record)
    if semantic_errors:
        raise RecordError("run-record validation failed: " + "; ".join(semantic_errors))
    if previous is not None:
        validate_run_record(previous, schema_path=schema_path)
        _validate_history(previous, record)


def new_run_record(
    spec_id: str,
    effective_policy_snapshot: Mapping[str, Any],
    *,
    run_id: str | None = None,
    spec_revision: str | None = None,
    spec_digests: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create a new run snapshot without mutating the supplied effective policy."""
    now = _now()
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "spec_id": spec_id,
        "run_id": run_id or _new_id("run"),
        "status": "running",
        "started_at": now,
        "checkpointed_at": now,
        "spec_snapshot": {
            "revision": spec_revision,
            "digests": dict(spec_digests or {}),
        },
        "effective_policy_snapshot": copy.deepcopy(dict(effective_policy_snapshot)),
        "tasks": {},
        "assignments": [],
        "candidates": [],
        "evidence": [],
        "findings": [],
        "review_counters": [],
        "rca_attempts": [],
        "operator_decisions": [],
        "git": {
            "base_commit": None,
            "integration_branch": None,
            "current_commit": None,
            "pull_requests": [],
            "state_ref": None,
            "state_revision": 0,
        },
    }
    validate_run_record(record)
    return record


def add_candidate(
    record: dict[str, Any],
    manifest: Mapping[str, Any],
    source_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a content-addressed candidate manifest."""
    manifest_copy = copy.deepcopy(dict(manifest))
    provenance_copy = copy.deepcopy(dict(source_provenance or {}))
    candidate_id = _candidate_digest(manifest_copy)
    for existing in record["candidates"]:
        if existing["candidate_id"] == candidate_id:
            if existing["manifest"] != manifest_copy:
                raise RecordError("candidate digest collision")
            return cast(dict[str, Any], existing)
    candidate = {
        "candidate_id": candidate_id,
        "manifest": manifest_copy,
        "source_provenance": provenance_copy,
        "created_at": _now(),
    }
    record["candidates"].append(candidate)
    validate_run_record(record)
    return candidate


def prepare_assignment(
    record: dict[str, Any],
    task_id: str,
    method: str,
    role_profile: str,
    *,
    capacity: int = 1,
    review_candidate_id: str | None = None,
    finding_id: str | None = None,
    rca_base_candidate_id: str | None = None,
    rca_attempt_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Persist an assignment and reserve capacity before any host launch."""
    if any(
        item["task_id"] == task_id and item["reservation_active"]
        for item in record["assignments"]
    ):
        raise RecordError(f"task {task_id} already has an unresolved assignment")
    now = _now()
    assignment_id = _new_id("asg")
    attempt: dict[str, Any] = {
        "id": _new_id("att"),
        "status": "prepared",
        "created_at": now,
    }
    dispatch_context: dict[str, Any] = {}
    if review_candidate_id is not None:
        if review_candidate_id not in {
            item["candidate_id"] for item in record["candidates"]
        }:
            raise RecordError("review dispatch candidate is not persisted")
        dispatch_context["candidate_id"] = review_candidate_id
    if finding_id is not None:
        if finding_id not in {item["id"] for item in record["findings"]}:
            raise RecordError("dispatch finding does not exist")
        dispatch_context["finding_id"] = finding_id
    if rca_base_candidate_id is not None:
        if role_profile != "rca" or rca_base_candidate_id not in {
            item["candidate_id"] for item in record["candidates"]
        }:
            raise RecordError("RCA dispatch requires a persisted baseline candidate")
        dispatch_context["rca_base_candidate_id"] = rca_base_candidate_id
    if role_profile == "rca_reviewer":
        dispatch_context["rca_attempt_ids"] = list(rca_attempt_ids)
    if dispatch_context:
        attempt["result"] = dispatch_context
    assignment = {
        "id": assignment_id,
        "task_id": task_id,
        "method": method,
        "role_profile": role_profile,
        "status": "prepared",
        "reserved_capacity": capacity,
        "reservation_active": True,
        "created_at": now,
        "attempts": [attempt],
    }
    task = record["tasks"].setdefault(
        task_id,
        {
            "status": "pending",
            "assignment_ids": [],
            "accepted_candidate_id": None,
        },
    )
    task["status"] = "assigned"
    task["assignment_ids"].append(assignment_id)
    record["assignments"].append(assignment)
    validate_run_record(record)
    return assignment


def _assignment_and_attempt(
    record: dict[str, Any], assignment_id: str, attempt_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    for assignment in record["assignments"]:
        if assignment["id"] != assignment_id:
            continue
        if not assignment["attempts"] or assignment["attempts"][-1]["id"] != attempt_id:
            raise RecordError("only the latest attempt can be changed")
        return assignment, assignment["attempts"][-1]
    raise RecordError(f"unknown assignment {assignment_id}")


def bind_launch(
    record: dict[str, Any], assignment_id: str, attempt_id: str, host_handle: str
) -> None:
    """Bind the host's returned handle to the already-persisted assignment."""
    assignment, attempt = _assignment_and_attempt(record, assignment_id, attempt_id)
    if (
        attempt["status"] not in {"prepared", "unknown"}
        or not assignment["reservation_active"]
    ):
        raise RecordError("launch can bind only to an unresolved reserved attempt")
    if attempt.get("host_handle") is not None:
        raise RecordError("attempt is already bound to a host handle")
    if not host_handle.strip():
        raise RecordError("host handle must not be empty")
    if any(
        prior.get("host_handle") == host_handle
        for item in record["assignments"]
        for prior in item["attempts"]
    ):
        raise RecordError(f"duplicate host handle {host_handle}")
    attempt["host_handle"] = host_handle
    attempt["status"] = "launched"
    assignment["status"] = "launched"
    record["tasks"][assignment["task_id"]]["status"] = "in_progress"
    validate_run_record(record)


def record_attempt_result(
    record: dict[str, Any],
    assignment_id: str,
    attempt_id: str,
    outcome: str,
    result: Mapping[str, Any] | None = None,
) -> None:
    """Record an attempt result without replacing a terminal outcome later."""
    if outcome not in {"unknown", *TERMINAL_ATTEMPT_STATES}:
        raise RecordError(f"invalid attempt outcome {outcome}")
    assignment, attempt = _assignment_and_attempt(record, assignment_id, attempt_id)
    current_status = attempt["status"]
    allowed = {
        "prepared": {"unknown", "failed", "cancelled"},
        "launched": {"unknown", "succeeded", "failed", "cancelled"},
        "unknown": {"succeeded", "failed", "cancelled"},
    }
    if outcome not in allowed.get(current_status, set()):
        raise RecordError(
            f"attempt cannot transition from {current_status} to {outcome}"
        )
    existing_result = attempt.get("result", {})
    if not isinstance(existing_result, Mapping):
        raise RecordError("attempt dispatch context must be an object")
    result_data = copy.deepcopy(dict(existing_result))
    for key, value in (result or {}).items():
        if key in DISPATCH_CONTEXT_FIELDS and key in result_data:
            if result_data[key] != value:
                raise RecordError(f"attempt {key} cannot change after dispatch")
        result_data[key] = copy.deepcopy(value)
    if result is not None or result_data:
        attempt["result"] = result_data
    attempt["status"] = outcome
    assignment["status"] = outcome
    assignment["reservation_active"] = outcome in RESERVED_ATTEMPT_STATES
    if outcome in RESERVED_ATTEMPT_STATES:
        record["tasks"][assignment["task_id"]]["status"] = "in_progress"
    elif outcome == "succeeded":
        record["tasks"][assignment["task_id"]]["status"] = "in_progress"
    else:
        record["tasks"][assignment["task_id"]]["status"] = "pending"
    validate_run_record(record)


def append_retry(record: dict[str, Any], assignment_id: str) -> dict[str, Any]:
    """Append a new prepared attempt after the previous attempt is terminal."""
    assignment = next(
        (item for item in record["assignments"] if item["id"] == assignment_id), None
    )
    if assignment is None:
        raise RecordError(f"unknown assignment {assignment_id}")
    if assignment["attempts"][-1]["status"] not in TERMINAL_ATTEMPT_STATES:
        raise RecordError("cannot retry while the previous attempt is unresolved")
    if any(
        item["task_id"] == assignment["task_id"]
        and item["id"] != assignment_id
        and item["reservation_active"]
        for item in record["assignments"]
    ):
        raise RecordError(
            f"task {assignment['task_id']} already has an active assignment"
        )
    attempt: dict[str, Any] = {
        "id": _new_id("att"),
        "status": "prepared",
        "created_at": _now(),
    }
    latest_result = assignment["attempts"][-1].get("result", {})
    dispatch_context: dict[str, Any] = {}
    if isinstance(latest_result, Mapping):
        if assignment["method"] == "Review" and "candidate_id" in latest_result:
            dispatch_context["candidate_id"] = latest_result["candidate_id"]
        if "finding_id" in latest_result:
            dispatch_context["finding_id"] = latest_result["finding_id"]
        for field in ("rca_base_candidate_id", "rca_attempt_ids"):
            if field in latest_result:
                dispatch_context[field] = copy.deepcopy(latest_result[field])
    if dispatch_context:
        attempt["result"] = copy.deepcopy(dispatch_context)
    assignment["attempts"].append(attempt)
    assignment["status"] = "prepared"
    assignment["reservation_active"] = True
    record["tasks"][assignment["task_id"]]["status"] = "assigned"
    validate_run_record(record)
    return cast(dict[str, Any], assignment)


def reserved_capacity(record: Mapping[str, Any]) -> int:
    """Return capacity held by prepared, launched, or uncertain assignments."""
    return sum(
        assignment["reserved_capacity"]
        for assignment in record["assignments"]
        if assignment["reservation_active"]
    )


def reserve_task_repair(
    record: dict[str, Any], task_id: str, limit: int
) -> dict[str, Any]:
    """Consume one task-review repair when its assignment is reserved."""
    counter = next(
        (
            item
            for item in record["review_counters"]
            if item["scope_id"] == task_id and item["level"] == "task"
        ),
        None,
    )
    if counter is None:
        counter = {
            "scope_id": task_id,
            "level": "task",
            "repairs_used": 0,
            "limit": limit,
        }
        record["review_counters"].append(counter)
    elif counter["limit"] != limit:
        raise RecordError("task repair counter limit differs from policy snapshot")
    if counter["repairs_used"] >= counter["limit"]:
        raise RecordError(f"task repair limit reached for {task_id}")
    counter["repairs_used"] += 1
    validate_run_record(record)
    return counter


def reserve_rca_attempt(
    record: dict[str, Any], finding_id: str, limit: int
) -> dict[str, Any]:
    """Consume one RCA allowance when its finding-specific assignment is reserved."""
    counter = next(
        (item for item in record["rca_attempts"] if item["finding_id"] == finding_id),
        None,
    )
    if counter is None:
        counter = {"finding_id": finding_id, "attempts_used": 0, "limit": limit}
        record["rca_attempts"].append(counter)
    elif counter["limit"] != limit:
        raise RecordError("RCA counter limit differs from policy snapshot")
    if counter["attempts_used"] >= counter["limit"]:
        raise RecordError(f"RCA attempt limit reached for {finding_id}")
    counter["attempts_used"] += 1
    validate_run_record(record)
    return counter


def add_finding(
    record: dict[str, Any],
    task_id: str,
    summary: str,
    *,
    scope: str | None = None,
    blocking: bool = True,
    evidence_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Append a stable finding identity with an initial history entry."""
    now = _now()
    finding = {
        "id": _new_id("fnd"),
        "task_id": task_id,
        "summary": summary,
        "scope": scope or task_id,
        "blocking": blocking,
        "status": "open",
        "evidence_ids": list(evidence_ids),
        "history": [
            {
                "at": now,
                "action": "opened",
                "rationale": summary,
                "evidence_ids": list(evidence_ids),
            }
        ],
        "created_at": now,
    }
    record["findings"].append(finding)
    validate_run_record(record)
    return cast(dict[str, Any], finding)


def append_finding_evidence(
    record: dict[str, Any], finding_id: str, evidence_id: str, rationale: str
) -> dict[str, Any]:
    """Link repeat evidence to a stable finding and reopen it when needed."""
    finding = next(
        (item for item in record["findings"] if item["id"] == finding_id), None
    )
    if finding is None:
        raise RecordError(f"unknown finding {finding_id}")
    evidence = next(
        (item for item in record["evidence"] if item["id"] == evidence_id), None
    )
    if evidence is None:
        raise RecordError(f"unknown evidence {evidence_id}")
    if not rationale.strip():
        raise RecordError("finding evidence rationale must not be empty")
    if evidence_id in finding["evidence_ids"]:
        return cast(dict[str, Any], finding)
    action = "confirmed" if finding["status"] in {"open", "deferred"} else "reopened"
    finding["status"] = "open"
    if evidence_id not in finding["evidence_ids"]:
        finding["evidence_ids"].append(evidence_id)
    finding["history"].append(
        {
            "at": _now(),
            "action": action,
            "rationale": rationale.strip(),
            "evidence_ids": [evidence_id],
        }
    )
    validate_run_record(record)
    return cast(dict[str, Any], finding)


def latest_check_evidence(
    record: Mapping[str, Any], check_id: str, candidate_id: str
) -> Mapping[str, Any] | None:
    """Use append order, including unavailable/inconclusive check outcomes."""
    return next(
        (
            item
            for item in reversed(record["evidence"])
            if item["kind"] == "check"
            and item.get("check_id") == check_id
            and item["candidate_id"] == candidate_id
        ),
        None,
    )


def resolve_check_finding(
    record: dict[str, Any],
    finding_id: str,
    task_id: str,
    check_id: str,
    candidate_id: str,
    evidence_id: str,
) -> dict[str, Any]:
    """Resolve a check-scoped finding only with matching passing check evidence."""
    finding = next(
        (item for item in record["findings"] if item["id"] == finding_id), None
    )
    if finding is None:
        raise RecordError(f"unknown finding {finding_id}")
    if (
        finding["task_id"] != task_id
        or finding["scope"] != f"check:{check_id}"
        or not finding["blocking"]
        or finding["status"] not in {"open", "deferred"}
    ):
        raise RecordError("check resolution does not match an open task finding")
    evidence = latest_check_evidence(record, check_id, candidate_id)
    if (
        evidence is None
        or evidence["id"] != evidence_id
        or evidence["kind"] != "check"
        or evidence.get("check_id") != check_id
        or evidence["outcome"] != "pass"
        or evidence["candidate_id"] != candidate_id
    ):
        raise RecordError("check finding resolution requires matching passing evidence")
    finding["status"] = "resolved"
    if evidence_id not in finding["evidence_ids"]:
        finding["evidence_ids"].append(evidence_id)
    finding["history"].append(
        {
            "at": _now(),
            "action": "resolved",
            "rationale": f"Check {check_id} passed for the current candidate",
            "evidence_ids": [evidence_id],
        }
    )
    validate_run_record(record)
    return cast(dict[str, Any], finding)


def resolve_finding(
    record: dict[str, Any],
    finding_id: str,
    rationale: str,
    evidence_id: str,
    *,
    candidate_id: str,
) -> dict[str, Any]:
    """Resolve one open finding with an explicit passing evidence reference."""
    finding = next(
        (item for item in record["findings"] if item["id"] == finding_id), None
    )
    if finding is None:
        raise RecordError(f"unknown finding {finding_id}")
    if finding["scope"].startswith("check:"):
        raise RecordError(
            "check findings require latest matching passing check evidence"
        )
    if not finding["blocking"] or finding["status"] not in {"open", "deferred"}:
        raise RecordError(f"finding {finding_id} is not unresolved")
    if not rationale.strip():
        raise RecordError("finding resolution rationale must not be empty")
    evidence = next(
        (item for item in record["evidence"] if item["id"] == evidence_id), None
    )
    assignment = (
        next(
            (
                item
                for item in record["assignments"]
                if item["id"] == evidence.get("assignment_id")
            ),
            None,
        )
        if evidence is not None
        else None
    )
    if (
        evidence is None
        or evidence["kind"] != "review"
        or evidence["outcome"] != "pass"
        or evidence["candidate_id"] != candidate_id
        or assignment is None
        or assignment["task_id"] != finding["task_id"]
        or assignment["method"] != "Review"
    ):
        raise RecordError("finding resolution requires current passing review evidence")
    now = _now()
    finding["status"] = "resolved"
    finding["evidence_ids"].append(evidence_id)
    finding["history"].append(
        {
            "at": now,
            "action": "resolved",
            "rationale": rationale.strip(),
            "evidence_ids": [evidence_id],
        }
    )
    validate_run_record(record)
    return cast(dict[str, Any], finding)


def add_evidence(
    record: dict[str, Any],
    candidate_id: str,
    kind: str,
    outcome: str,
    summary: str,
    *,
    check_id: str | None = None,
    assignment_id: str | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    source_provenance: Mapping[str, Any] | None = None,
    tool_versions: Mapping[str, Any] | None = None,
    config_digest: str | None = None,
    check_definition_digest: str | None = None,
) -> dict[str, Any]:
    """Append evidence with the source and tool context observed by its check."""
    evidence: dict[str, Any] = {
        "id": _new_id("evd"),
        "candidate_id": candidate_id,
        "kind": kind,
        "outcome": outcome,
        "summary": summary,
        "created_at": _now(),
        "artifacts": [copy.deepcopy(dict(item)) for item in artifacts or ()],
        "source_provenance": copy.deepcopy(dict(source_provenance or {})),
        "tool_versions": copy.deepcopy(dict(tool_versions or {})),
    }
    if check_id is not None:
        evidence["check_id"] = check_id
    if assignment_id is not None:
        evidence["assignment_id"] = assignment_id
    if config_digest is not None:
        evidence["config_digest"] = config_digest
    if check_definition_digest is not None:
        evidence["check_definition_digest"] = check_definition_digest
    record["evidence"].append(evidence)
    validate_run_record(record)
    return evidence


def evidence_available(evidence: Mapping[str, Any], artifact_root: Path) -> bool:
    """Return false for unavailable evidence or a missing or stale required artifact."""
    if evidence["outcome"] == "unavailable":
        return False
    root = artifact_root.expanduser().resolve()
    for artifact in evidence["artifacts"]:
        if not artifact["required"]:
            continue
        relative = Path(artifact["path"])
        if relative.is_absolute() or ".." in relative.parts:
            return False
        try:
            path = (root / relative).resolve(strict=True)
            path.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return False
        if not path.is_file():
            return False
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if digest != artifact["sha256"]:
            return False
    return True
