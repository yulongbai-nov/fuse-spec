"""Contract tests for the skill-level Poteto route table and Fuse ownership."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).parents[1]
POTETO = ROOT / "skills" / "poteto-mode"
SCRIPT_DIR = ROOT / "skills" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
fuse_control: Any = importlib.import_module("fuse_control")
fuse_record: Any = importlib.import_module("fuse_record")


def route_contract() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((POTETO / "routes.json").read_text(encoding="utf-8")),
    )


@pytest.mark.parametrize(
    ("scenario", "method", "playbook"),
    [
        ("new_behavior", "Feature", "feature.md"),
        ("broken_existing_behavior", "Bug Fix", "bug-fix.md"),
        ("preserve_behavior_restructure", "Refactoring", "refactoring.md"),
        (
            "approved_measurable_performance_goal",
            "Performance Issue",
            "performance-issue.md",
        ),
        (
            "diagnose_accessible_live_process",
            "Runtime/Trace Forensics",
            "live-process-forensics.md",
        ),
        ("diagnose_existing_trace", "Runtime/Trace Forensics", "trace-forensics.md"),
        ("approved_observable_design_question", "Prototype", "prototype.md"),
        ("approved_exact_visual_equivalence", "Visual Parity", "visual-parity.md"),
    ],
)
def test_scenarios_select_the_documented_playbook(
    scenario: str, method: str, playbook: str
) -> None:
    contract = route_contract()
    routes = {item["scenario"]: item for item in contract["scenarios"]}
    selected = routes[scenario]
    assert selected["method"] == method
    assert selected["playbook"] == playbook
    assert (POTETO / "playbooks" / playbook).is_file()


def test_research_remains_a_fuse_phase_and_hillclimb_is_deferred() -> None:
    contract = route_contract()
    assert contract["research"]["phase"] == "research"
    assert contract["research"]["method"] is None
    assert "Investigation" in contract["excluded_methods"]
    assert "Hillclimb" in contract["deferred_methods"]


def test_route_table_contains_only_the_approved_methods() -> None:
    contract = route_contract()
    methods = {item["method"] for item in contract["routes"]}
    assert not methods.intersection(contract["excluded_methods"])
    assert not methods.intersection(contract["deferred_methods"])
    assert methods == fuse_control.IMPLEMENTATION_METHODS
    assert methods <= {item["method"] for item in contract["scenarios"]}


def test_selected_method_is_saved_before_the_assignment_is_launched() -> None:
    record = fuse_record.new_run_record(
        "001-demo", {"roles": {"worker": {"model": "latest-luna"}}, "limits": {}}
    )
    assignment = fuse_record.prepare_assignment(record, "T1", "Feature", "worker")

    assert assignment["task_id"] == "T1"
    assert assignment["method"] == "Feature"
    assert assignment["status"] == "prepared"
    assert assignment["attempts"][0]["status"] == "prepared"


def test_conditional_routes_carry_their_required_evidence() -> None:
    contract = route_contract()
    by_method = {item["method"]: item for item in contract["routes"]}
    assert {
        method for method, route in by_method.items() if route.get("conditional")
    } == {
        "Performance Issue",
        "Runtime/Trace Forensics",
        "Prototype",
        "Visual Parity",
    }
    assert contract["decision_contract"]["rationale_max_chars"] == 500
    assert contract["decision_contract"]["evidence_ref_prefix"] == "source:"
    assert "baseline" in by_method["Performance Issue"]["evidence"]
    assert "post_change" in by_method["Performance Issue"]["evidence"]
    assert by_method["Runtime/Trace Forensics"]["source_routes"] == {
        "live_process": "live-process-forensics.md",
        "existing_trace": "trace-forensics.md",
    }
    assert by_method["Prototype"]["isolated"] is True
    assert by_method["Visual Parity"]["requires_exact_visual_requirement"] is True


def test_requirement_model_defaults_match_the_latest_role_policy() -> None:
    requirements = (
        ROOT / ".specs" / "001-pstack-integration" / "requirements.md"
    ).read_text(encoding="utf-8")
    r7 = requirements.split("### R7: Choose suitable workers and models", 1)[1].split(
        "### R8:", 1
    )[0]
    clauses = {
        line.split(". ", 1)[0]: line
        for line in r7.splitlines()
        if line[:2] in {"4.", "5.", "6.", "7."}
    }

    assert "latest Luna" in clauses["4"] and "Max reasoning" in clauses["4"]
    assert "latest Astra" in clauses["4"] and "manager" in clauses["4"]
    assert "latest Luna" in clauses["5"] and "Max reasoning" in clauses["5"]
    assert "latest Luna" in clauses["6"] and "Max reasoning" in clauses["6"]
    assert "latest Luna" in clauses["7"] and "Max reasoning" in clauses["7"]
    assert "latest Sol" not in clauses["6"]
    assert "SHALL assign Astra with Max" not in clauses["4"]
    assert "separate Astra agent" not in clauses["7"]
    assert "Luna Max does not imply unlimited usage" in r7


def test_skill_files_make_spec_execute_the_only_lifecycle_owner() -> None:
    coordinator = (ROOT / "skills" / "spec-execute" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    router = (POTETO / "SKILL.md").read_text(encoding="utf-8")
    worker = (ROOT / "skills" / "agents" / "poteto-agent.md").read_text(
        encoding="utf-8"
    )
    comment_reviewer = (ROOT / "skills" / "agents" / "comment-sicko.md").read_text(
        encoding="utf-8"
    )

    assert "routes.json" in router
    assert "poteto-mode/SKILL.md" in coordinator
    assert "fuse_launch.py" in coordinator
    assert "--repo-root" in coordinator and "--spec-dir" in coordinator
    assert "prepare-dispatch" in coordinator
    assert "reroute" in coordinator.lower()
    assert "shared transition validator" in coordinator.lower()
    assert "optional" in coordinator.lower() and "hook" in coordinator.lower()
    assert "requirement/design references" in coordinator
    assert "required check IDs" in coordinator
    assert "--route-rationale" in coordinator
    assert "--route-evidence-ref" in coordinator
    assert "--source-route" in coordinator
    assert "keyword heuristics" in coordinator
    assert "cannot accept" in worker.lower()
    assert "one owner" in comment_reviewer.lower()
    assert (
        coordinator.index("Select one method")
        < coordinator.index("Call `prepare-dispatch`")
        < coordinator.index("Launch the worker")
    )
