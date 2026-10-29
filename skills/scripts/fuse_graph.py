#!/usr/bin/env python3
"""Validate task/check dependency graphs and return deterministic ready work."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from plan_waves import parse_task_declarations
else:
    try:
        from .plan_waves import parse_task_declarations
    except ImportError:
        from plan_waves import parse_task_declarations

TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CHECK_ID = re.compile(r"^[a-z][a-z0-9_-]*$")


class GraphError(ValueError):
    """Raised when approved task/check dependencies are ambiguous or invalid."""


def parse_task_dependencies(tasks_markdown: str) -> dict[str, list[str]]:
    """Read task IDs and dependencies without treating checkboxes as acceptance."""
    declarations, duplicates, _done = parse_task_declarations(tasks_markdown)
    if duplicates:
        raise GraphError("duplicate task id " + ", ".join(sorted(duplicates)))
    graph = {task_id: task["deps"] for task_id, task in declarations.items()}
    return validate_dependency_graph(graph, "task")


def validate_dependency_graph(
    graph: Mapping[str, Sequence[str]], label: str
) -> dict[str, list[str]]:
    """Validate IDs and edges before any graphlib scheduling operation."""
    errors: list[str] = []
    node_ids = set(graph)
    normalized: dict[str, list[str]] = {}
    id_pattern = TASK_ID if label == "task" else CHECK_ID
    for node_id in sorted(graph):
        if not id_pattern.fullmatch(node_id):
            errors.append(f"invalid {label} id {node_id}")
        dependencies = list(graph[node_id])
        if len(dependencies) != len(set(dependencies)):
            repeated = sorted(
                {
                    dependency
                    for dependency in dependencies
                    if dependencies.count(dependency) > 1
                }
            )
            reference_name = "prerequisite" if label == "check" else "dependency"
            errors.extend(
                f"duplicate {reference_name} {dependency} for {label} {node_id}"
                for dependency in repeated
            )
        for dependency in dependencies:
            if dependency == node_id:
                errors.append(f"{label} {node_id} depends on itself")
            elif dependency not in node_ids:
                noun = "task" if label == "task" else "prerequisite"
                errors.append(
                    f"{label} {node_id} references unknown {noun} {dependency}"
                )
        normalized[node_id] = sorted(set(dependencies) & node_ids)
    if errors:
        raise GraphError("; ".join(errors))
    try:
        TopologicalSorter(normalized).prepare()
    except CycleError as error:
        graph_name = (
            "task dependency graph" if label == "task" else "check prerequisite graph"
        )
        raise GraphError(f"{graph_name} contains a cycle") from error
    return normalized


def validate_check_graph(
    checks: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Validate the prerequisite edges declared by project check definitions."""
    dependencies: dict[str, list[str]] = {}
    for check_id, check in checks.items():
        raw_prerequisites = check.get("prerequisites", ())
        if not isinstance(raw_prerequisites, Sequence) or isinstance(
            raw_prerequisites, str
        ):
            raise GraphError(f"check {check_id} prerequisites must be a list")
        if any(not isinstance(item, str) for item in raw_prerequisites):
            raise GraphError(f"check {check_id} prerequisites must be string IDs")
        dependencies[check_id] = list(raw_prerequisites)
    return validate_dependency_graph(dependencies, "check")


def applicable_check_graph(
    checks: Mapping[str, Mapping[str, Any]], applicable: Iterable[str]
) -> dict[str, list[str]]:
    """Return the applicable check graph plus all transitive prerequisites."""
    graph = validate_check_graph(checks)
    requested = list(applicable)
    if len(requested) != len(set(requested)):
        repeated = sorted({item for item in requested if requested.count(item) > 1})
        raise GraphError("duplicate applicable check " + ", ".join(repeated))
    unknown = sorted(set(requested) - set(graph))
    if unknown:
        raise GraphError("unknown applicable check " + ", ".join(unknown))

    included: set[str] = set()
    pending = list(requested)
    while pending:
        check_id = pending.pop()
        if check_id in included:
            continue
        included.add(check_id)
        pending.extend(graph[check_id])
    return {
        check_id: [
            dependency for dependency in graph[check_id] if dependency in included
        ]
        for check_id in sorted(included)
    }


def _ready_nodes(
    graph: Mapping[str, Sequence[str]],
    completed: Iterable[str],
    blocked: Iterable[str] = (),
) -> list[str]:
    """Advance only completed nodes, leaving blocked paths closed."""
    blocked_set = set(blocked)
    completed_set = set(completed) - blocked_set
    sorter = TopologicalSorter(graph)
    sorter.prepare()
    ready: list[str] = []
    while sorter.is_active():
        batch = sorted(sorter.get_ready())
        if not batch:
            break
        releasable = [node_id for node_id in batch if node_id in completed_set]
        ready.extend(
            node_id
            for node_id in batch
            if node_id not in completed_set and node_id not in blocked_set
        )
        if releasable:
            sorter.done(*releasable)
    return sorted(ready)


def accepted_task_ids(run_record: Mapping[str, Any]) -> list[str]:
    """Return acceptance that is not suspended by an unresolved reservation."""
    active = {
        assignment.get("task_id")
        for assignment in run_record.get("assignments", ())
        if assignment.get("reservation_active") is True
    }
    return sorted(
        task_id
        for task_id, state in run_record.get("tasks", {}).items()
        if state.get("status") == "accepted"
        and state.get("accepted_candidate_id")
        and task_id not in active
    )


def ready_task_ids(
    task_graph: Mapping[str, Sequence[str]], run_record: Mapping[str, Any]
) -> list[str]:
    """Return tasks ready from declarations and accepted execution-record state."""
    graph = validate_dependency_graph(task_graph, "task")
    task_states = run_record.get("tasks", {})
    if not isinstance(task_states, Mapping):
        raise GraphError("run record task state must be a mapping")
    unknown_states = sorted(set(task_states) - set(graph))
    if unknown_states:
        raise GraphError(
            "run record contains undeclared task " + ", ".join(unknown_states)
        )

    active_tasks = {
        assignment.get("task_id")
        for assignment in run_record.get("assignments", ())
        if assignment.get("reservation_active") is True
    }
    accepted = set(accepted_task_ids(run_record))
    blocked: set[str] = set()
    for task_id in graph:
        state = task_states.get(task_id, {})
        status = state.get("status", "pending")
        if task_id not in accepted and status in {
            "blocked",
            "cancelled",
            "assigned",
            "in_progress",
            "accepted",
        }:
            blocked.add(task_id)
    blocked.update(task_id for task_id in active_tasks if isinstance(task_id, str))
    return _ready_nodes(graph, accepted, blocked)


def ready_check_ids(
    checks: Mapping[str, Mapping[str, Any]],
    applicable: Iterable[str],
    *,
    completed: Iterable[str],
    blocked: Iterable[str] = (),
) -> list[str]:
    """Return stable check IDs ready under applicability and evidence state."""
    graph = applicable_check_graph(checks, applicable)
    return _ready_nodes(graph, completed, blocked)
