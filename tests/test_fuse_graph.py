"""Tests for validated task and check dependency readiness."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_DIR = Path(__file__).parents[1] / "skills" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
fuse_graph: Any = importlib.import_module("fuse_graph")


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        (
            "- [ ] T1 First [id:T1 deps: impl:R1]\n"
            "- [ ] T1 Duplicate [id:T1 deps: impl:R1]\n",
            "duplicate task id T1",
        ),
        ("- [ ] T1 Missing [id:T1 deps:T9 impl:R1]\n", "unknown task T9"),
        ("- [ ] T1 Self [id:T1 deps:T1 impl:R1]\n", "depends on itself"),
        (
            "- [ ] T1 First [id:T1 deps:T2 impl:R1]\n"
            "- [ ] T2 Second [id:T2 deps:T1 impl:R1]\n",
            "task dependency graph contains a cycle",
        ),
        (
            "- [ ] T1 Duplicate dep [id:T1 deps:T2,T2 impl:R1]\n"
            "- [ ] T2 Dep [id:T2 deps: impl:R1]\n",
            "duplicate dependency T2",
        ),
    ],
)
def test_task_graph_rejects_invalid_references(tasks: str, message: str) -> None:
    with pytest.raises(fuse_graph.GraphError, match=message):
        fuse_graph.parse_task_dependencies(tasks)


def test_task_readiness_uses_accepted_run_state_not_checkboxes() -> None:
    task_graph = fuse_graph.parse_task_dependencies(
        """\
- [x] T1 Legacy checked [id:T1 deps: impl:R1]
- [ ] T2 Depends on T1 [id:T2 deps:T1 impl:R1]
- [ ] T3 Independent [id:T3 deps: impl:R1]
- [ ] T4 Blocked by T5 [id:T4 deps:T5 impl:R1]
- [ ] T5 Blocked [id:T5 deps: impl:R1]
"""
    )
    run_record = {
        "tasks": {
            "T1": {
                "status": "accepted",
                "assignment_ids": [],
                "accepted_candidate_id": "a" * 64,
            },
            "T5": {
                "status": "blocked",
                "assignment_ids": [],
                "accepted_candidate_id": None,
            },
        },
        "assignments": [],
    }

    assert fuse_graph.ready_task_ids(task_graph, run_record) == ["T2", "T3"]


def test_unresolved_assignment_blocks_only_its_task() -> None:
    task_graph: dict[str, list[str]] = {"T1": [], "T2": []}
    run_record = {
        "tasks": {"T1": {"status": "in_progress"}},
        "assignments": [
            {"task_id": "T1", "reservation_active": True},
        ],
    }

    assert fuse_graph.ready_task_ids(task_graph, run_record) == ["T2"]


@pytest.mark.parametrize(
    ("checks", "message"),
    [
        ({"lint": {"prerequisites": ["missing"]}}, "unknown prerequisite missing"),
        ({"lint": {"prerequisites": ["lint"]}}, "check lint depends on itself"),
        (
            {
                "lint": {"prerequisites": ["tests"]},
                "tests": {"prerequisites": ["lint"]},
            },
            "check prerequisite graph contains a cycle",
        ),
        (
            {
                "lint": {"prerequisites": ["tests", "tests"]},
                "tests": {"prerequisites": []},
            },
            "duplicate prerequisite tests",
        ),
    ],
)
def test_check_graph_rejects_invalid_references(
    checks: dict[str, dict[str, list[str]]], message: str
) -> None:
    with pytest.raises(fuse_graph.GraphError, match=message):
        fuse_graph.validate_check_graph(checks)


def test_check_readiness_closes_prerequisites_and_is_stable() -> None:
    checks = {
        "tests": {"prerequisites": []},
        "lint": {"prerequisites": ["tests"]},
        "docs": {"prerequisites": []},
        "integration": {"prerequisites": ["lint"]},
    }

    assert fuse_graph.ready_check_ids(
        checks, ["integration", "docs"], completed=[], blocked=["tests"]
    ) == ["docs"]
    assert fuse_graph.ready_check_ids(
        checks, ["integration", "docs"], completed=["tests"]
    ) == ["docs", "lint"]
    assert fuse_graph.ready_check_ids(
        checks, ["integration", "docs"], completed=["tests", "lint", "docs"]
    ) == ["integration"]


def test_check_readiness_rejects_unknown_or_duplicate_applicability() -> None:
    checks: dict[str, dict[str, list[str]]] = {"tests": {"prerequisites": []}}

    with pytest.raises(fuse_graph.GraphError, match="unknown applicable check missing"):
        fuse_graph.ready_check_ids(checks, ["missing"], completed=[])
    with pytest.raises(fuse_graph.GraphError, match="duplicate applicable check tests"):
        fuse_graph.ready_check_ids(checks, ["tests", "tests"], completed=[])
